"""Workspace discovery, clarification and verified delivery tools."""
from dataclasses import asdict
import csv
import json
import re
from typing import Literal
from urllib.parse import quote

from pydantic import Field, model_validator
from packages.contracts.agent import AgentError, AgentModel, AgentRef, AgentToolResult, Identity
from packages.contracts.models import AssetSetItem, SourceLocator
from workbench_platform.agent.registry import ToolSpec
from workbench_platform.agent.store import canonical, digest
from workbench_platform.application_services.decisions import AskUser, ask_user


COMPLETION_RULE_VERSION = 'delivery-obligations-v3'


class Empty(AgentModel):
    pass


class ToolQuery(AgentModel):
    keyword: str = Field(default='', max_length=100)
    family: str = Field(default='', max_length=80)
    capability: str = Field(default='', max_length=80)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=30)


class ToolDescribe(AgentModel):
    names: list[str] = Field(min_length=1, max_length=15)


class ToolSearch(AgentModel):
    query: str = Field(min_length=1, max_length=240)
    limit: int = Field(default=6, ge=1, le=15)


class AssetSearch(AgentModel):
    keyword: str = Field(default='', max_length=100)
    media_type: str = Field(default='', max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=50)


class AssetDescribe(AgentModel):
    asset_ids: list[Identity] = Field(min_length=1, max_length=50)


class AssetSetCreate(AssetDescribe):
    pass


class DeliveryCheck(AgentModel):
    name: Literal['row_count', 'column_trimmed', 'column_text_equals', 'column_decimal_places', 'column_prefix']
    bundle_id: Identity
    filename: str = Field(min_length=1, max_length=256)
    column: str | None = Field(default=None, min_length=1, max_length=256)
    expected_rows: int | None = Field(default=None, ge=0, le=2**53 - 1)
    expected_value: str | None = Field(default=None, max_length=4096)
    places: int | None = Field(default=None, ge=0, le=30)
    prefix: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode='after')
    def required_parameters(self):
        required = {'row_count': {'expected_rows'}, 'column_trimmed': {'column'},
                    'column_text_equals': {'column', 'expected_value'},
                    'column_decimal_places': {'column', 'places'}, 'column_prefix': {'column', 'prefix'}}[self.name]
        supplied = {field for field in ('column', 'expected_rows', 'expected_value', 'places', 'prefix')
                    if getattr(self, field) is not None}
        if supplied != required:
            raise ValueError('验收条件缺少必要参数或包含不适用的参数')
        return self


class Delivery(AgentModel):
    summary: str = Field(min_length=1, max_length=2000)
    finalize: bool = True
    artifact_refs: list[AgentRef] = Field(default_factory=list, max_length=50)
    result_refs: list[AgentRef] = Field(default_factory=list, max_length=50)
    checks: list[DeliveryCheck] = Field(default_factory=list, max_length=50)
    require_review: bool = False


class ArtifactGet(AgentModel):
    bundle_id: Identity


def safe_asset(asset):
    return {key: value for key, value in asset.items() if key not in {'source', 'source_id'}}


def context(args, ctx):
    project = ctx.services.workspace_store.get_workspace(ctx.workspace_id)
    registry = ctx.services.asset_library[0]
    selected = [safe_asset(registry.describe(ctx.workspace_id, asset_id)) for asset_id in ctx.selected_assets]
    data = {'workspace': asdict(project), 'selected_assets': selected,
            'authorized_reference_ids': (ctx.services.agent.tasks.get(ctx.session_id).get('authorized_reference_ids', [])
                                         if hasattr(ctx.services.agent, 'tasks') else []),
            'initial_refs': ctx.store.session(ctx.session_id).get('initial_refs', []),
            'data_sessions': ctx.services.data_workbench.list_page(ctx.workspace_id, limit=20)['items'],
            'capabilities': [ctx.services.agent.registry.describe(name, ctx.services)['availability'] |
                             {'name': name} for name in ctx.services.agent.registry.tools]}
    return AgentToolResult(status='success', summary='已读取当前项目和明确选中的资料范围', data=data).model_dump()


def list_tools(args, ctx):
    page = ctx.services.agent.registry.catalog(keyword=args.keyword, family=args.family,
        capability=args.capability, offset=args.offset, limit=args.limit, services=ctx.services)
    return AgentToolResult(status='success', summary='已读取工具目录；需要调用时用 tools_describe 加载指定名称',
        data={key: value for key, value in page.items() if key != 'next_offset'},
        next_offset=page['next_offset']).model_dump()


def search_tools(args, ctx):
    matches = ctx.services.agent.registry.search(args.query, limit=args.limit, services=ctx.services)
    return AgentToolResult(status='success', summary=f'已找到并加载{len(matches)}个工具；下一步可以直接调用',
        data={'tools': matches, 'activated_tools': [item['name'] for item in matches]}).model_dump()


def describe_tools(args, ctx):
    # The actual input schema is included once, in the next native API tools
    # field. Output schema is enforced by the dispatcher rather than duplicated
    # in every subsequent historical tool message.
    tools = [ctx.services.agent.registry.describe(name, ctx.services) for name in dict.fromkeys(args.names)]
    return AgentToolResult(status='success', summary='已请求加载指定工具；下一轮原生工具字段提供准确输入契约',
        data={'tools': [{key: value for key, value in item.items()
                        if key not in {'input_schema', 'output_schema', 'examples'}} for item in tools],
              'activated_tools': [item['name'] for item in tools]}).model_dump()


def models(args, ctx):
    view = ctx.services.agent.connection.view()
    items = [{'id': view.get('model', ''), 'role': 'controller', 'tool_family': 'agent',
              'workspace_id': ctx.workspace_id, 'input_kinds': ['messages', 'tool_results'],
              'supported_tasks': ['tool_selection'] if view.get('available') else [],
              'version_ref': {'connection_revision': view.get('revision', 0), 'model': view.get('model')},
              'device': 'configured_endpoint', 'limits': ['仅通过该连接原生工具探针的配置可用'],
              'ready': view.get('available', False), 'protocol_tested': view.get('capabilities', {}),
              'reason': view.get('reason', '尚未配置主控')}]
    projection = getattr(ctx.services.agent, 'model_projection', None)
    if projection:
        items.extend(projection(ctx))
    return AgentToolResult(status='success', summary='模型角色与就绪状态来自实际配置及原生服务', data={'models': items}).model_dump()


def search(args, ctx):
    registry = ctx.services.asset_library[0]
    # The selected scope is applied before pagination, never after returning a
    # page. A bounded input list (<=100) needs no unbounded workspace scan.
    if ctx.selected_assets:
        values = [registry.describe(ctx.workspace_id, identifier) for identifier in ctx.selected_assets]
        values = [value for value in values if args.keyword.casefold() in value['original_name'].casefold()
                  and (not args.media_type or value['media_type'] == args.media_type)]
        items, total = values[args.offset:args.offset + args.limit], len(values)
    else:
        items, total = registry.list(ctx.workspace_id, keyword=args.keyword, media_type=args.media_type,
                                    offset=args.offset, limit=args.limit)
    return AgentToolResult(status='success', summary='已查询当前范围的真实资料',
        data={'items': [safe_asset(value) for value in items], 'total': total},
        next_offset=args.offset + args.limit if args.offset + args.limit < total else None).model_dump()


def describe_assets(args, ctx):
    from workbench_platform.application_services.references import asset_allowed
    if any(not asset_allowed(ctx, identifier) for identifier in args.asset_ids):
        raise ValueError('资料超出选中范围')
    return AgentToolResult(status='success', summary='已读取所选资料版本',
        data={'items': [safe_asset(ctx.services.asset_library[0].describe(ctx.workspace_id, identifier))
                        for identifier in args.asset_ids]}).model_dump()


def create_set(args, ctx):
    describe_assets(args, ctx)
    registry, sets, _ = ctx.services.asset_library
    items = []
    for identifier in args.asset_ids:
        asset = registry.get(ctx.workspace_id, identifier)
        locator = SourceLocator(schema_version='1.0', kind='file', asset_id=identifier,
                                asset_revision=asset.revision, blob_hash=asset.blob_hash, precision='unknown')
        items.append(AssetSetItem(schema_version='1.0', item_id='i' + digest([ctx.operation_id, identifier])[:32],
            asset_id=identifier, asset_revision=asset.revision, blob_hash=asset.blob_hash, source_locator=locator))
    asset_set = sets.create(ctx.workspace_id, items, asset_set_id='s' + ctx.operation_id[1:])
    return AgentToolResult(status='success', summary='已固定不可变资料集', data=asset_set.model_dump(mode='json'),
        result_refs=[AgentRef(kind='asset_set', workspace_id=ctx.workspace_id, object_id=asset_set.asset_set_id,
                             revision=asset_set.revision, sha256=asset_set.manifest_hash)]).model_dump()


def get_artifact(args, ctx):
    bundle = ctx.services.artifact_center.get(ctx.workspace_id, args.bundle_id)
    return AgentToolResult(status='success', summary='已读取真实成果', data=bundle,
        artifact_refs=[AgentRef(kind='artifact', workspace_id=ctx.workspace_id, object_id=args.bundle_id, revision=1)]).model_dump()


def artifacts(args, ctx):
    return AgentToolResult(status='success', summary='已读取当前项目成果目录',
        data=ctx.services.artifact_center.list(ctx.workspace_id, args.offset, args.limit)).model_dump()


def _delivery_check_result(check, status, message, evidence):
    return {'name': check.name, 'status': status, 'message': message,
            'bundle_id': check.bundle_id, 'filename': check.filename,
            **({'column': check.column} if check.column is not None else {}),
            'requirement': check.model_dump(exclude_none=True, exclude={'name', 'bundle_id', 'filename', 'column'}),
            'evidence': evidence}


def _csv_delivery_checks(path, artifact, bundle_id, filename, checks, ctx):
    """One streaming scan per CSV; preserve text and collect bounded diagnostics."""
    measured_rows, columns, states = 0, [], []
    base = {'artifact_id': artifact.artifact_id, 'artifact_revision': artifact.revision,
            'sha256': artifact.blob_hash, 'size_bytes': artifact.size_bytes}
    try:
        with path.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream, strict=True)
            columns = next(reader, [])
            if not columns or any('\x00' in name for name in columns):
                raise csv.Error('Missing or invalid header')
            for check in checks:
                state = {'check': check, 'column_index': None, 'failure': None, 'failed_rows': 0,
                         'failure_rows': [], 'matched_rows': 0}
                if check.column is not None:
                    matches = [index for index, name in enumerate(columns) if name == check.column]
                    if not matches:
                        matches = [index for index, name in enumerate(columns)
                                   if '|' in name and name.rsplit('|', 1)[1].strip() == check.column]
                    if len(matches) != 1:
                        state['failure'] = '验收列不存在或列名后缀不唯一'
                    else:
                        state['column_index'] = matches[0]
                if check.name == 'column_decimal_places':
                    suffix = r'\.[0-9]{' + str(check.places) + '}' if check.places else ''
                    state['decimal_pattern'] = re.compile(r'[+-]?[0-9]+' + suffix + r'\Z')
                states.append(state)
            for row in reader:
                if not row:  # Blank physical lines do not create data records.
                    continue
                if len(row) != len(columns) or any('\x00' in value for value in row):
                    raise csv.Error('Invalid row shape or content')
                measured_rows += 1
                if measured_rows % 4096 == 0:
                    ctx.current()
                for state in states:
                    check = state['check']
                    if check.name == 'row_count' or state['failure']:
                        continue
                    value = row[state['column_index']]
                    valid = (value == value.strip() if check.name == 'column_trimmed' else
                             value == check.expected_value if check.name == 'column_text_equals' else
                             bool(state['decimal_pattern'].fullmatch(value)) if check.name == 'column_decimal_places' else
                             value.startswith(check.prefix))
                    state['matched_rows' if valid else 'failed_rows'] += 1
                    if not valid and len(state['failure_rows']) < 5:
                        state['failure_rows'].append(measured_rows)
        evidence = {**base, 'measured_rows': measured_rows, 'column_count': len(columns), 'complete': True}
        outcomes = [{'name': 'csv_readback', 'status': 'measured', 'bundle_id': bundle_id, 'filename': filename,
                     'message': f'已完整读回 CSV：{measured_rows} 行、{len(columns)} 列；测量值不代表已匹配未声明的要求',
                     'evidence': evidence}]
        for state in states:
            check = state['check']
            if check.name == 'row_count':
                passed = measured_rows == check.expected_rows
                details = dict(evidence)
                message = f'实际 {measured_rows} 行，要求 {check.expected_rows} 行'
            else:
                passed = not state['failure'] and state['failed_rows'] == 0
                details = {**evidence, 'matched_rows': state['matched_rows'], 'failed_rows': state['failed_rows'],
                           'failure_rows': state['failure_rows'],
                           'resolved_column': columns[state['column_index']] if state['column_index'] is not None else None}
                message = state['failure'] or f'已检查 {measured_rows} 行，匹配 {state["matched_rows"]} 行，不匹配 {state["failed_rows"]} 行'
            outcomes.append(_delivery_check_result(check, 'passed' if passed else 'failed', message, details))
        return outcomes
    except (csv.Error, UnicodeError, OSError):
        evidence = {**base, 'measured_rows': measured_rows, 'column_count': len(columns), 'complete': False}
        message = 'CSV 无法完整按 UTF-8 读回，或表头、列数、字段内容无效'
        return [{'name': 'csv_readback', 'status': 'failed', 'bundle_id': bundle_id, 'filename': filename,
                 'message': message, 'evidence': evidence},
                *[_delivery_check_result(check, 'failed', message, evidence) for check in checks]]


def _delivery_check_failure(outcomes):
    return AgentToolResult(status='error', summary='成果验收未通过，原始成果及检查证据保留，请修复后重新交付',
        error=AgentError(code='delivery_check_failed', message='成果内容未通过声明的验收条件或完整 CSV 读回'),
        data={'verified': False, 'finalized': False, 'delivery_checks': outcomes}).model_dump()


def _persistent_incompleteness(value):
    from workbench_platform.application_services.requirement_coverage import UNKNOWN_CODES
    if (value.get('error') or {}).get('code') in UNKNOWN_CODES:
        return True
    if any('未处理区域' in warning or '提取失败' in warning for warning in value.get('warnings', [])):
        return True
    def nonzero(coverage):
        return isinstance(coverage, dict) and any(type(coverage.get(key)) is int and coverage[key] > 0
                                                 for key in ('failed', 'cancelled', 'pending'))
    if nonzero(value.get('coverage')):
        return True
    data = value.get('data') or {}
    if data.get('native_status') in {'failed', 'cancelled', 'interrupted', 'unknown'}:
        return True
    def nested(item):
        if isinstance(item, dict):
            return any((key in {'coverage', 'counts'} and nonzero(detail)) or nested(detail)
                       for key, detail in item.items() if isinstance(detail, (dict, list)))
        return isinstance(item, list) and any(nested(detail) for detail in item)
    return nested(data)


def _operation_target(tool, arguments):
    """Conservative business identity, excluding only optimistic versions.

    A successful operation on a different object or different action/window
    cannot erase a failure. Native incomplete coverage is never superseded.
    """
    if tool == 'delivery_publish':
        return digest({key: ([AgentRef.model_validate(ref).model_dump() for ref in arguments.get(key, [])]
                             if key in {'artifact_refs', 'result_refs'} else arguments.get(key, []))
                       for key in ('artifact_refs', 'result_refs', 'checks')})
    if tool == 'audit_write_finding':
        if arguments.get('draft_id'):
            return digest({'draft_id': arguments['draft_id']})
        from .audit import FindingWrite
        return digest(FindingWrite.model_validate(arguments).model_dump(exclude={'expected_revision'}))
    if tool == 'data_import':
        from packages.contracts.data_operations import DataImport
        return digest(DataImport.model_validate(arguments).model_dump(exclude={'expected_version'}))
    if tool == 'ocr_inspect_document':
        return digest({key: arguments.get(key) for key in ('link_id', 'asset_set_id', 'asset_set_revision')})
    identity = {key: value for key, value in arguments.items()
                if key.endswith(('_id', '_ids')) and key not in {'request_id', 'parent_operation_id'} and value is not None}
    if not identity:
        return digest({key: value for key, value in arguments.items() if key not in {'expected_revision', 'expected_version'}})
    selectors = ('side', 'kind', 'action', 'stage', 'filename', 'category', 'engine', 'engines', 'model_id', 'role')
    identity.update({key: arguments[key] for key in selectors if key in arguments})
    if isinstance(arguments.get('step'), dict):
        identity['step'] = {key: arguments['step'][key] for key in ('operator', 'column', 'columns', 'predicate')
                            if key in arguments['step']}
    if tool.startswith(('data_read_', 'ocr_read_', 'classification_read_', 'results_')):
        identity.update({key: arguments[key] for key in ('offset', 'limit', 'columns', 'result_id') if key in arguments})
    return digest(identity)


def _same_check(old, current):
    fields = ('name', 'bundle_id', 'filename', 'column', 'requirement')
    if any(old.get(key) != current.get(key) for key in fields):
        return False
    expected_hash = old.get('evidence', {}).get('sha256')
    return expected_hash is None or expected_hash == current.get('evidence', {}).get('sha256')


def _refused_pdf_imports(records, ctx):
    """Prove a narrowly rejected PDF import never acquired native intent.

    Inspection creates a derived input set before handoff validation. Every
    native import, however, requires a durable handoff row first. A missing
    row across all links is useful only when the old link is not a registered
    OCR target and a later inspection succeeded for the exact fixed inputs.
    """
    from contextlib import closing
    import sqlite3
    ctx.current()
    candidates = []
    for index, record in enumerate(records):
        value, arguments = record['result'], record['arguments']
        if (record['tool'] != 'ocr_inspect_document' or value['status'] != 'error'
                or (value.get('error') or {}).get('code') != 'invalid_input'
                or _persistent_incompleteness(value)
                or value.get('data')
                or any(value.get(key) for key in ('job_refs', 'result_refs', 'artifact_refs'))
                or not isinstance(record['id'], str) or not record['id'].startswith('o')
                or set(arguments) != {'link_id', 'asset_set_id', 'asset_set_revision'}
                or not isinstance(arguments.get('link_id'), str) or not arguments['link_id']
                or not isinstance(arguments.get('asset_set_id'), str) or not arguments['asset_set_id']
                or type(arguments.get('asset_set_revision')) is not int or arguments['asset_set_revision'] < 1):
            continue
        later = next((item for item in records[index + 1:] if item['tool'] == 'ocr_inspect_document'
            and item['result']['status'] == 'success' and not _persistent_incompleteness(item['result'])
            and item['arguments'].get('asset_set_id') == arguments['asset_set_id']
            and item['arguments'].get('asset_set_revision') == arguments['asset_set_revision']), None)
        if later is not None:
            candidates.append((record, later))
    if not candidates:
        return {}
    try:
        links = {link.link_id for link in ctx.services.workspace_store.list_links(ctx.workspace_id)
                 if link.app_id == 'ocr'}
        path = ctx.services.ocr_handoff.db_path.resolve()
        proved = {}
        # mode=ro neither creates a missing database nor changes an intent.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            for record, later in candidates:
                if record['arguments'].get('link_id') in links or later['arguments'].get('link_id') not in links:
                    continue
                attempt_set_id = 's' + record['id'][1:]
                accepted = db.execute('SELECT 1 FROM ocr_handoff WHERE workspace_id=? AND asset_set_id=? LIMIT 1',
                                     (ctx.workspace_id, attempt_set_id)).fetchone()
                if accepted is None:
                    proved[record['id']] = {'attempt_set_id': attempt_set_id,
                        'input_set_id': record['arguments']['asset_set_id'],
                        'input_set_revision': record['arguments']['asset_set_revision'],
                        'satisfied_by_operation': later['id'], 'native_import_intent': 'absent'}
    except (sqlite3.Error, OSError, ValueError, KeyError, AttributeError, TypeError):
        proved = {}
    ctx.current()
    return proved


def _delivery_failures(records, current, current_id, *, requirement_coverage=None,
                       tool_effects=None, verified_final=False, nonblocking_operations=None,
                       refused_pdf_imports=None):
    """Project unresolved failures; immutable operation history stays intact."""
    candidates = [*records, {'id': current_id, 'tool': 'delivery_publish', 'arguments': current['arguments'],
                             'result': {'status': 'success', 'data': {'delivery_checks': current['outcomes']}}}]
    failed, repaired = [], []
    native_repairs = {item['operation_id']: item for item in (requirement_coverage or {}).get('repaired_operations', [])}
    for index, record in enumerate(records):
        value = record['result']
        if record['tool'] == 'delivery_publish' and (value.get('error') or {}).get('code') == 'delivery_requirements_conflict':
            # A rejected attempt cannot lower the persisted requirements. The
            # final check still inherits and re-reads every locked stage, so a
            # parameter refusal is not an unresolved native coverage failure.
            continue
        persistent = _persistent_incompleteness(value)
        if value['status'] not in {'error', 'unsupported'} and not persistent:
            continue
        # A refused read is immutable history, not a failed business attempt.
        # This distinction is available only after the current final delivery
        # has actually reverified its fixed references and native obligations.
        code = (value.get('error') or {}).get('code')
        reader_failure = (value.get('data') or {}).get('failure') or {}
        reader_prepare_refused = (code == 'native_reader_unavailable'
            and set(value.get('data') or {}) == {'failure'}
            and isinstance(reader_failure, dict)
            and reader_failure.get('stage') == 'native_reader_prepare'
            and reader_failure.get('application') == 'ocr' and reader_failure.get('accepted_write') is False
            and not any(value.get(key) for key in ('job_refs', 'result_refs', 'artifact_refs')))
        nonblocking_reason = None
        if (verified_final and not persistent and value['status'] == 'error'
                and (tool_effects or {}).get(record['tool']) == 'read'):
            if code in {'invalid_input', 'revision_conflict'}:
                nonblocking_reason = 'read_request_refused'
            elif reader_prepare_refused:
                nonblocking_reason = 'native_reader_service_restored'
            elif record['tool'] == 'conversation_answer' and code == 'delivery_required':
                nonblocking_reason = 'delivery_precondition_satisfied'
        if (verified_final and current.get('references_verified') and not persistent
                and record['tool'] == 'delivery_publish' and value['status'] == 'error'
                and requirement_coverage is not None and requirement_coverage.get('missing') == 0
                and not requirement_coverage.get('unverified')
                and ((code == 'invalid_input' and not value.get('data')) or reader_prepare_refused)
                and not any(value.get(key) for key in ('job_refs', 'result_refs', 'artifact_refs'))):
            # A refused delivery argument is not an accepted native write.
            # Current fixed files and all native requirements were reverified;
            # the old refusal remains auditable, without poisoning completion.
            nonblocking_reason = 'native_reader_service_restored' if reader_prepare_refused else 'delivery_request_refused'
        pdf_refusal = (refused_pdf_imports or {}).get(record['id'])
        if (verified_final and not persistent and value['status'] == 'error'
                and record['tool'] == 'ocr_inspect_document' and code == 'invalid_input'
                and not any(value.get(key) for key in ('job_refs', 'result_refs', 'artifact_refs')) and pdf_refusal):
            nonblocking_reason = 'pre_import_rejected'
        if nonblocking_reason:
            if nonblocking_operations is not None:
                nonblocking_operations.append({'operation_id': record['id'], 'tool': record['tool'],
                    'error_code': code, 'reason': nonblocking_reason, 'verified_by_delivery': current_id,
                    **({'proof': pdf_refusal} if nonblocking_reason == 'pre_import_rejected' else {})})
            continue
        if (record['id'] in native_repairs
                and (value.get('error') or {}).get('code') not in {'response_unknown', 'unknown_response', 'receipt_unknown', 'native_response_unknown', 'engine_response_unknown'}
                and not any('未处理区域' in warning for warning in value.get('warnings', []))):
            repaired.append(native_repairs[record['id']])
            continue
        repair = None
        if not persistent:
            for later in candidates[index + 1:]:
                if later['tool'] != record['tool'] or later['result']['status'] != 'success' or _persistent_incompleteness(later['result']):
                    continue
                if record['tool'] == 'delivery_publish' and (value.get('error') or {}).get('code') == 'delivery_check_failed':
                    failures = [outcome for outcome in value.get('data', {}).get('delivery_checks', []) if outcome['status'] == 'failed']
                    successes = [outcome for outcome in later['result'].get('data', {}).get('delivery_checks', [])
                                 if outcome['status'] in {'passed', 'measured'}]
                    matches = bool(failures) and all(any(_same_check(old, new) for new in successes) for old in failures)
                else:
                    matches = _operation_target(record['tool'], record['arguments']) == _operation_target(later['tool'], later['arguments'])
                if matches:
                    repair = later['id']
                    break
        if repair:
            repaired.append({'operation_id': record['id'], 'tool': record['tool'], 'repaired_by': repair})
        else:
            failed.append(value)
    return failed, repaired


def _delivery_review(args, files, published, readable_text, ctx):
    """A delivery approval comes from the user decision for these fixed bytes."""
    fingerprint = digest({'artifacts': sorted((ref.model_dump(mode='json') for ref in args.artifact_refs), key=canonical),
        'results': sorted((ref.model_dump(mode='json') for ref in args.result_refs), key=canonical),
        'files': sorted(files, key=canonical)})
    action = {'action': 'review_delivery', 'fingerprint': fingerprint}
    data = {'delivery': published, 'readable_text': readable_text, 'verified': False, 'finalized': False,
            'draft': True, 'review_state': 'unreviewed', 'action': action}
    decisions = ctx.store.rows("SELECT d.id,d.reply,d.request_id,o.result FROM decisions d JOIN operations o ON o.id=d.id "
        "WHERE d.run_id=? AND d.kind='delivery_review' AND o.tool='delivery_publish' AND o.result IS NOT NULL "
        'ORDER BY o.created DESC,o.rowid DESC', (ctx.run_id,))
    for decision in decisions:
        recorded = (json.loads(decision['result']).get('data') or {}).get('action') or {}
        if recorded != action:
            continue
        if decision['reply'] is None:
            return AgentToolResult(status='needs_user', summary='草稿已保存，等待您确认当前固定版本后定稿',
                artifact_refs=args.artifact_refs, result_refs=args.result_refs,
                data={**data, 'decision_id': decision['id'], 'revision': 1,
                      'options': ['确认此版本', '不确认']}).model_dump(), None
        trusted_reply = decision['request_id'] and ctx.store.rows("SELECT 1 FROM events WHERE run_id=? AND identity=? "
            "AND kind='message' AND json_extract(payload,'$.role')='user' "
            "AND json_extract(payload,'$.decision_id')=?", (ctx.run_id, decision['request_id'], decision['id']))
        if trusted_reply and decision['reply'] == '确认此版本':
            return None, {'fingerprint': fingerprint, 'decision_id': decision['id'],
                          'request_id': decision['request_id'], 'reply': decision['reply']}
        return AgentToolResult(status='error', summary='当前固定版本尚未获得真实用户确认；草稿可下载，不能作为核准成果',
            artifact_refs=args.artifact_refs, result_refs=args.result_refs, data=data,
            error=AgentError(code='review_required', message='请保留未复核草稿；只有真实用户确认当前版本后才能定稿')).model_dump(), None
    question = ask_user(AskUser(question='草稿已生成并可下载审阅。是否确认当前固定版本，并允许将这批成果定稿？',
                               options=['确认此版本', '不确认']), ctx)
    with ctx.store.transaction() as db:
        db.execute("UPDATE decisions SET kind='delivery_review' WHERE id=? AND run_id=?", (ctx.operation_id, ctx.run_id))
    question.update(artifact_refs=[ref.model_dump() for ref in args.artifact_refs],
                    result_refs=[ref.model_dump() for ref in args.result_refs])
    question['data'].update(data)
    return question, None


def delivery(args, ctx):
    from .. import delivery_staging as staging
    request = args
    previous = staging.replay(request, ctx)
    if previous is not None:
        return previous
    stages, stages_fingerprint = staging.snapshot(ctx)
    from ..delivery_policy import effective_policy
    policy = effective_policy(staging._tasks(ctx).get(ctx.session_id))
    try:
        args = Delivery.model_validate(staging.merge(request, stages))
        if args.finalize and policy['require_review']:
            args = args.model_copy(update={'require_review': True})
    except staging.RequirementsConflict as error:
        return AgentToolResult(status='error', summary=str(error),
            error=AgentError(code='delivery_requirements_conflict', message=str(error)),
            data={'verified': False, 'finalized': False}).model_dump()
    if not args.artifact_refs and not args.result_refs:
        raise ValueError('交付必须引用实际结果或成果')
    published, bundles, allowed_files, outcomes, readable_text = [], {}, {}, [], []
    fixed_artifacts, fixed_results, checked_files, verified_results = [], [], [], []
    review_snapshots, has_draft, needs_delivery_decision = [], False, False
    from workbench_platform.application_services.references import verify
    from workbench_platform.application_services.review_provenance import inspect_review_provenance
    def blocked_review():
        if args.require_review:
            from .multimodal import checked_snapshot, Result, request_action, UserAction
            for identity in review_snapshots:
                value = checked_snapshot(ctx, Result(job_id=identity['job_id'], snapshot_id=identity['snapshot_id']))
                if (value['analysis'].get('review') or {}).get('assessment') != 'confirmed':
                    return request_action(UserAction(action='review', job_id=value['job_id'], snapshot_id=value['snapshot_id']), ctx)
        return _delivery_check_failure(outcomes)
    for reference in args.artifact_refs:
        if reference.workspace_id != ctx.workspace_id or reference.kind != 'artifact':
            raise ValueError('跨项目成果')
        bundle = verify(reference.model_dump(), ctx)
        review = inspect_review_provenance(reference, ctx, verified=bundle, require_review=args.require_review)
        outcomes.extend(review['checks'])
        review_snapshots.extend(review['snapshots'])
        has_draft |= review['draft']
        # Native fixed exports retain their existing actual review path.
        # A general report cannot borrow a source result's review as its own.
        needs_delivery_decision |= bundle.get('kind') != 'manual_analysis' or not review['snapshots']
        fixed_artifacts.append(reference.model_copy(update={'sha256': bundle['content_sha256']}))
        bundles[bundle['bundle_id']] = bundle
        for file in bundle['files']:
            allowed_files[(bundle['bundle_id'], file['name'])] = file
    for check in args.checks:
        file = allowed_files.get((check.bundle_id, check.filename))
        if file is None or not (check.filename.casefold().endswith('.csv') or file.get('media_type') == 'text/csv'):
            return _delivery_check_failure([_delivery_check_result(check, 'failed',
                '验收必须指向本次交付成果引用中的 CSV 文件', {})])
    for bundle in bundles.values():
        files = []
        for file in bundle['files']:
            artifact = ctx.services.artifact_center.file(ctx.workspace_id, bundle['bundle_id'], file['name'])
            checked_files.append({'bundle_id': bundle['bundle_id'], 'filename': file['name'],
                'artifact_id': artifact.artifact_id, 'revision': artifact.revision,
                'sha256': artifact.blob_hash, 'size_bytes': artifact.size_bytes})
            # Verify actual bytes, rather than equating metadata with delivery.
            with ctx.services.artifact_center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                if file['name'].casefold().endswith('.csv') or artifact.media_type == 'text/csv':
                    checks = [check for check in args.checks if check.bundle_id == bundle['bundle_id'] and check.filename == file['name']]
                    outcomes.extend(_csv_delivery_checks(path, artifact, bundle['bundle_id'], file['name'], checks, ctx))
            files.append({**file, 'download_url': f'/api/platform/v1/workspaces/{ctx.workspace_id}/artifacts/'
                f'{bundle["bundle_id"]}/files/{quote(file["name"])}'})
        published.append({'bundle_id': bundle['bundle_id'], 'title': bundle.get('title', '成果'), 'files': files})
    if any(outcome['status'] == 'failed' for outcome in outcomes):
        return blocked_review()
    expected = staging.expected_files(stages, set(bundles))
    actual = {(file['bundle_id'], file['filename']): file for file in checked_files}
    if any(actual.get(key) != file for key, file in expected.items()):
        return AgentToolResult(status='error', summary='已分批核验的成果文件身份或哈希发生变化，请核实原成果',
            error=AgentError(code='delivery_reference_changed', message='固定成果文件身份或哈希变化'),
            data={'verified': False, 'finalized': False, 'delivery_checks': outcomes}).model_dump()
    for reference in args.result_refs:
        value = verify(reference.model_dump(), ctx)
        review = inspect_review_provenance(reference, ctx, verified=value, require_review=args.require_review)
        outcomes.extend(review['checks'])
        review_snapshots.extend(review['snapshots'])
        has_draft |= review['draft']
        needs_delivery_decision |= not (reference.application == 'multimodal' and reference.kind == 'result'
                                        and review['snapshots'])
        verified_results.append(value)
        if reference.application == 'multimodal' and reference.kind == 'result':
            from .multimodal import result_ref
            fixed_results.append(result_ref(ctx, value))
        else:
            fixed_results.append(reference.model_copy(update={'sha256': value['content_sha256']})
                                 if isinstance(value, dict) and value.get('content_sha256') else reference)
        if reference.kind == 'result' and reference.application == 'ocr':
            from workbench_platform.artifacts.text_outputs import publish_text_result
            output = publish_text_result(ctx, reference, verified=value)
            readable_text.append(output['readable_text'])
            text_bundle = output['delivery']
            if not any(bundle['bundle_id'] == text_bundle['bundle_id'] for bundle in published):
                published.append(text_bundle)
                for file in text_bundle['files']:
                    artifact = ctx.services.artifact_center.file(ctx.workspace_id, text_bundle['bundle_id'], file['name'])
                    with ctx.services.artifact_center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                            expected_revision=artifact.revision, expected_hash=artifact.blob_hash):
                        pass
                    checked_files.append({'bundle_id': text_bundle['bundle_id'], 'filename': file['name'],
                        'artifact_id': artifact.artifact_id, 'revision': artifact.revision,
                        'sha256': artifact.blob_hash, 'size_bytes': artifact.size_bytes})
    # Persist only explicitly staged result requirements. Fixed artifact refs
    # retain their exact sidecars and derive the same native proof on every
    # final verification, without consuming the model's result-ref input limit.
    staged_results = list(fixed_results)
    from workbench_platform.application_services.requirement_coverage import delivered_text_artifacts
    text_artifacts = delivered_text_artifacts(fixed_artifacts, ctx, verified_objects=bundles.values())
    def result_identity(reference):
        return staging._ref_key(reference.model_dump() if hasattr(reference, 'model_dump') else reference)
    fixed_identities = {result_identity(item) for item in fixed_results}
    readable_identities = {result_identity(item['reference']) for item in readable_text}
    for output in text_artifacts:
        reference = output['reference']
        if result_identity(reference) not in fixed_identities:
            fixed_results.append(reference)
            verified_results.append(output['verified'])
            fixed_identities.add(result_identity(reference))
        if result_identity(reference) not in readable_identities:
            readable_text.append(output['readable_text'])
            readable_identities.add(result_identity(reference))
    args = args.model_copy(update={'artifact_refs': fixed_artifacts, 'result_refs': fixed_results})
    staged_args = args.model_copy(update={'result_refs': staged_results})
    if any(outcome['status'] == 'failed' for outcome in outcomes):
        return blocked_review()
    exported = {bundle.get('snapshot_id') for bundle in bundles.values() if bundle.get('snapshot_id')}
    for value in verified_results:
        if not isinstance(value, dict) or not value.get('snapshot_id') or value['snapshot_id'] in exported:
            continue
        native = ctx.services.multimodal_service
        snapshot_id, job_id = value['snapshot_id'], value['job_id']
        _, files = native.snapshot_export(ctx.workspace_id, job_id, snapshot_id)
        base = f'/api/platform/v1/workspaces/{ctx.workspace_id}/multimodal/jobs/{job_id}/snapshots/{snapshot_id}'
        delivered_files = []
        for name, path in files.items():
            checksum, size = native.file_hash(path), path.stat().st_size
            checked_files.append({'bundle_id': snapshot_id, 'filename': name, 'revision': value['revision'],
                                  'sha256': checksum, 'size_bytes': size})
            delivered_files.append({'name': name, 'sha256': checksum, 'size_bytes': size,
                                    'download_url': base + '/files/' + quote(name)})
        published.append({'bundle_id': snapshot_id, 'kind': 'multimodal_snapshot', 'snapshot_id': snapshot_id,
            'native_job_id': job_id, 'title': ('未复核草稿 · ' if not value['review_revision'] else '') + value['kind'],
            'files': delivered_files, 'download_url': base + '/download'})
        exported.add(snapshot_id)
    if not args.finalize:
        draft = has_draft or policy['require_review'] or args.require_review
        result = AgentToolResult(status='success', summary=('已分批核验本批成果并保存验收要求；尚未最终交付。' + args.summary)[:2000],
            artifact_refs=args.artifact_refs, result_refs=args.result_refs,
            data={'delivery': published, 'verified': True, 'finalized': False,
                  'readable_text': readable_text,
                  'draft': draft, 'review_state': 'unreviewed' if draft else 'not_required_or_confirmed',
                  'delivery_checks': outcomes, 'staged': True}).model_dump()
        return staging.commit(request, staged_args, checked_files, result, stages_fingerprint, ctx,
                              provisional=has_draft and policy['require_review'])
    calls = ctx.store.rows('SELECT o.id,o.tool,o.arguments,o.result FROM operations o JOIN runs r ON r.id=o.run_id '
                          'WHERE r.session_id=? AND o.result IS NOT NULL ORDER BY o.created,o.rowid', (ctx.session_id,))
    records = [{**row, 'arguments': json.loads(row['arguments']), 'result': json.loads(row['result'])} for row in calls]
    task_receipts = ctx.store.rows('SELECT o.result FROM operations o JOIN runs r ON r.id=o.run_id '
                                  'WHERE r.session_id=? AND o.id!=?', (ctx.session_id, ctx.operation_id))
    outstanding = [row for row in task_receipts if row['result'] is None
                   or json.loads(row['result'])['status'] in {'pending', 'needs_verification'}]
    if outstanding:
        raise ValueError('仍有未完成任务')
    from ..requirement_coverage import project_requirements
    requirement_coverage = project_requirements(records, [*args.artifact_refs, *args.result_refs], ctx,
        verified_objects=[*bundles.values(), *verified_results])
    requirement_coverage['text_artifact_proofs'] = [output['proof'] for output in text_artifacts]
    from ..delivery_jobs import verify_native_jobs, NativeDeliveryBlocked
    try:
        native_checks, native_limitations = verify_native_jobs([*args.artifact_refs, *args.result_refs], ctx,
            verified_objects=[*bundles.values(), *verified_results, *review_snapshots], requirement_coverage=requirement_coverage)
    except NativeDeliveryBlocked as error:
        return AgentToolResult(status='error', summary=str(error)[:2000],
            error=AgentError(code='native_delivery_unverified', message=str(error)[:500]),
            data={'verified': False, 'finalized': False, 'delivery_checks': [*outcomes, *error.checks]}).model_dump()
    outcomes.extend(native_checks)
    registry = getattr(getattr(ctx.services, 'agent', None), 'registry', None)
    tool_effects = {name: spec.effect for name, spec in registry.tools.items()} if registry is not None else {}
    nonblocking_operations = []
    failed, repaired = _delivery_failures(records, {'arguments': request.model_dump(), 'outcomes': outcomes,
        'references_verified': bool(args.artifact_refs or args.result_refs)}, ctx.operation_id,
        requirement_coverage=requirement_coverage, tool_effects=tool_effects, verified_final=True,
        nonblocking_operations=nonblocking_operations, refused_pdf_imports=_refused_pdf_imports(records, ctx))
    review_receipt = None
    if args.require_review and needs_delivery_decision:
        blocked, review_receipt = _delivery_review(args, checked_files, published, readable_text, ctx)
        if blocked is not None:
            return blocked
    result = AgentToolResult(status='success', summary=args.summary, artifact_refs=args.artifact_refs,
        result_refs=args.result_refs, data={'delivery': published, 'readable_text': readable_text,
        'requirement_coverage': requirement_coverage,
        'completeness': 'partial' if failed or native_limitations or requirement_coverage['missing'] or requirement_coverage['unverified'] else 'complete',
        'verified': True, 'finalized': True, 'delivery_checks': outcomes,
        'completion_rule_version': COMPLETION_RULE_VERSION, 'nonblocking_operations': nonblocking_operations,
        'draft': has_draft, 'review_state': 'unreviewed' if has_draft else 'confirmed' if review_receipt else 'not_required_or_confirmed',
        **({'delivery_review': review_receipt} if review_receipt else {}),
        'repaired_operations': repaired,
        'limitations': [*[value['summary'] for value in failed], *native_limitations]}).model_dump()
    return staging.commit(request, staged_args, checked_files, result, stages_fingerprint, ctx)


def register(registry):
    for name, schema, handler, description, effect in [
        ('workspace_get_context', Empty, context, '读取当前项目、已选资料及作业范围；不猜测对象ID。', 'read'),
        ('tools_list', ToolQuery, list_tools, '分页检索已注册工具的轻量目录；发现不会加载契约，调用前用tools_describe。', 'read'),
        ('tools_describe', ToolDescribe, describe_tools, '显式加载指定工具，下一轮可调用；完整输入契约仅在原生tools字段提供。', 'read'),
        ('models_list', Empty, models, '读取主控、OCR引擎与分类模型的角色和实际就绪状态。', 'read'),
        ('assets_search', AssetSearch, search, '在当前项目或明确已选资料中有界查询文件。', 'read'),
        ('assets_describe', AssetDescribe, describe_assets, '读取实际资料ID、版本和内容哈希，不返回本机路径。', 'read'),
        ('assets_create_set', AssetSetCreate, create_set, '将当前范围资料固定为不可变输入集，不修改人工标签。', 'derive'),
        ('ask_user', AskUser, ask_user, '仅在工作表、业务键等会实质改变结果的歧义时提问；回复续接原会话。', 'derive'),
        ('artifacts_list', AssetSearch, artifacts, '读取当前项目已有真实成果目录。', 'read'),
        ('artifacts_get', ArtifactGet, get_artifact, '读取实际成果及文件元数据。', 'read'),
        ('delivery_publish', Delivery, delivery, '核验成果与CSV条件。多文件逐件传finalize=false保存固定引用、哈希及checks，不结束任务；最后finalize=true可仅传summary，服务器合并全部已存批次重新读回核验，不能省略或降低已存要求。checks支持行数、去空格、精确文本、小数位数和前缀。', 'derive'),
        ('tools_search', ToolSearch, search_tools, '按目标、能力或工具名搜索专业工具并直接加载定义；搜索不执行处理，下一步无需再调用tools_describe。', 'read'),
    ]:
        registry.register(ToolSpec(name, description, schema, handler, effect=effect, idempotent=True))
