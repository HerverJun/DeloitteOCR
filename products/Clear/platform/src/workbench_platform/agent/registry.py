"""Strict executable tool registry with server-only context and operation IDs."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import asyncio
import inspect
import json
import re
from typing import Callable, Literal
from urllib.parse import quote

from pydantic import BaseModel
from packages.contracts.agent import AgentCall, AgentToolResult
from .store import canonical, digest, Conflict, RunSuperseded


def compact_schema(value):
    """Remove display annotations, never validation keywords or definition names."""
    if isinstance(value, list):
        return [compact_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    output = {}
    for key, item in value.items():
        if key in {'title', 'examples', '$comment'}:
            continue
        # A property/definition may itself be named title or examples.
        if key in {'properties', '$defs', 'definitions', 'patternProperties'}:
            output[key] = {name: compact_schema(schema) for name, schema in item.items()}
        else:
            output[key] = compact_schema(item)
    return output


from workbench_platform.application_services.context import ToolContext


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: type[BaseModel]
    execute: Callable
    version: str = '1'
    output_schema: type[BaseModel] = AgentToolResult
    effect: Literal['read', 'derive', 'versioned', 'control'] = 'read'
    input_kinds: tuple[str, ...] = ()
    output_kinds: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    limits: dict = field(default_factory=lambda: {'page_items': 50})
    availability: Callable | None = None
    prepare: Callable | None = None
    resource_keys: Callable = lambda args, ctx: ()
    lookup_operation: Callable | None = None
    idempotent: bool = False
    capability: str = ''
    aliases: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


async def invoke(handler, *arguments):
    if inspect.iscoroutinefunction(handler):
        return await handler(*arguments)
    result = await asyncio.to_thread(handler, *arguments)
    return await result if inspect.isawaitable(result) else result


def error_result(code, message, *, status='error', data=None):
    return AgentToolResult(status=status, summary=message, data=data or {},
                           error={'code': code, 'message': message, 'retryable': False}).model_dump(mode='json')


def input_failure(error):
    from pydantic import ValidationError
    if isinstance(error, ValidationError):
        fields = [{'field': '.'.join(map(str, item['loc'])), 'reason': item['type']}
                  for item in error.errors(include_input=False, include_context=False, include_url=False)[:20]]
        return error_result('invalid_arguments', '参数未通过工具契约，请修正列出的字段', data={'fields': fields})
    message = str(error)
    reasons = [
        (('资料版本已改变', 'revision', 'version', '版本'), 'revision_conflict', '对象版本已改变，请读取当前版本后修订新调用'),
        (('Column is unavailable', 'column', '列ID', '列名'), 'unknown_column', '指定列不可用，请读取真实表头和列ID后修正'),
        (('跨项目', '资料不在', '授权', 'scope'), 'scope_denied', '引用不属于当前项目或任务范围，请选择已授权资料'),
        (('工作表', 'sheet'), 'ambiguous_sheet', '请查询实际工作表；多个工作表时明确选择'),
        (('页集', '页超出', '页码'), 'invalid_pages', '页码范围无效，请读取实际PDF页数后修正'),
        (('引擎不可用', '不可用引擎', '没有可用', 'not available'), 'capability_unavailable', '所需能力尚不可用，请核对实际引擎、模型或扩展连接状态'),
    ]
    for needles, code, explanation in reasons:
        if any(needle in message for needle in needles):
            return error_result(code, explanation)
    field = str(error.args[0]) if isinstance(error, KeyError) and error.args else None
    return error_result('invalid_input', '输入或引用无效，请重新读取对应对象并核对工具参数',
        data={'missing_field': field} if field and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', field) else {})


def tool_activity(context, call, phase, result=None, *, summary=None):
    """Persist a public transition under the executing generation's fence.

    Parameters and native internals never enter this event. Before intent
    validation a blocked attempt has no operation identity and never claims a
    tool started. Duplicate callbacks/recovery retain the same transition ID.
    """
    value = result or {}
    payload = {'version': 1, 'generation': context.generation,
        'call_id': call['call_id'], 'operation_id': context.operation_id or None,
        'tool': call['tool'], 'phase': phase,
        'status': value.get('status', 'running' if phase == 'started' else 'error'),
        'summary': str(summary if summary is not None else value.get('summary', '工具开始执行'))[:500]}
    if value.get('job_refs'):
        payload['job_refs'] = value['job_refs']
    context.store.public_event(context.run_id, context.generation,
        'tool-activity:' + digest([context.run_id, call['call_id'], context.operation_id, phase, payload]),
        'tool_activity', payload)


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, ToolSpec] = {}
        self.capabilities = {}
        self.tool_guard = None

    def register_capability(self, specification):
        self.capabilities[specification.id] = specification

    def capability_catalog(self, services=None):
        from .capabilities import project
        return project(self, services)

    def register(self, specification: ToolSpec):
        AgentCall(call_id='registration', tool=specification.name, arguments={})
        if specification.name in self.tools or not callable(specification.execute):
            raise ValueError('Duplicate tool or missing handler')
        if specification.input_schema.model_config.get('extra') != 'forbid':
            raise ValueError('Tool input must forbid extra parameters')
        self.tools[specification.name] = specification

    def describe(self, name, services=None):
        from .capabilities import metadata
        spec = self.tools[name]
        availability = spec.availability(services) if spec.availability and services else {'state': 'ready'}
        return {'name': name, 'version': spec.version, 'description': spec.description,
                'input_schema': spec.input_schema.model_json_schema(),
                'output_schema': spec.output_schema.model_json_schema(),
                'effect': spec.effect, 'input_kinds': spec.input_kinds, 'output_kinds': spec.output_kinds,
                'examples': spec.examples, 'limits': spec.limits, 'availability': availability,
                'has_handler': True, 'has_lookup': spec.lookup_operation is not None, **metadata(spec)}

    def schemas(self, visible):
        return {name: {**compact_schema(self.tools[name].input_schema.model_json_schema()),
                       'description': self.tools[name].description} for name in visible if name in self.tools}

    def catalog(self, *, keyword='', family='', capability='', offset=0, limit=20, services=None):
        """A discovery page is metadata only; it grants no executable schema."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError('工具目录分页无效')
        needle = keyword.casefold()
        from .capabilities import metadata
        names = [name for name, spec in self.tools.items()
                 if (not family or name.split('_', 1)[0] == family)
                 and (not capability or metadata(spec)['capability'] == capability)
                 and (not needle or needle in ' '.join([name, spec.description, *metadata(spec)['aliases']]).casefold())]
        items = []
        for name in names[offset:offset + limit]:
            spec = self.tools[name]
            availability = spec.availability(services) if spec.availability and services else {'state': 'ready'}
            items.append({'name': name, 'description': spec.description[:240],
                          **metadata(spec), 'effect': spec.effect,
                          'availability': availability})
        return {'tools': items, 'total': len(names), 'offset': offset,
                'next_offset': offset + limit if offset + limit < len(names) else None,
                'families': sorted({name.split('_', 1)[0] for name in self.tools}),
                'capabilities': self.capability_catalog(services)}

    def active_tools(self, visible, *, core=(), protected=()):
        """Stable activation order allows oldest unrelated schemas to be reclaimed."""
        names = sorted(visible) if isinstance(visible, (set, frozenset)) else list(visible)
        return list(dict.fromkeys(name for name in [*core, *names, *protected] if name in self.tools))

    def search(self, query, *, limit=6, services=None):
        """Rank the allowed local catalog without invoking any business handler."""
        from .capabilities import metadata
        query = query.casefold().strip()
        words = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+', query)
        terms = set(words)
        for word in words:
            if re.search(r'[\u4e00-\u9fff]', word):
                terms.update(word[index:index + 2] for index in range(min(len(word) - 1, 60)))
        ranked = []
        for name, spec in self.tools.items():
            if name in {'tools_search', 'tools_list', 'tools_describe', 'conversation_answer'}:
                continue
            meta = metadata(spec)
            haystack = ' '.join([name, spec.description, meta['capability'], *meta['aliases']]).casefold()
            score = 100 if query == name or name in terms else 0
            score += 20 if query and query in haystack else 0
            score += sum((4 if term in name else 1) for term in terms if term in haystack)
            if score:
                availability = spec.availability(services) if spec.availability and services else {'state': 'ready'}
                ranked.append((score, name, {'name': name, 'description': spec.description,
                    'effect': spec.effect, 'availability': availability, **meta}))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in ranked[:limit]]

    def validate_batch(self, calls, visible):
        if len(calls) > 20 or len({item.get('call_id') for item in calls}) != len(calls):
            raise ValueError('调用批次无效')
        if len(calls) > 1 and any(item.get('tool') == 'conversation_answer' for item in calls):
            raise ValueError('对话回复必须单独调用，不能与未执行的业务操作同时提交')
        validated = []
        for item in calls:
            call = AgentCall.model_validate(item)
            if call.tool not in self.tools or call.tool not in visible:
                raise ValueError('工具未注册或尚未加载；请先使用 tools_describe 显式加载')
            arguments = self.tools[call.tool].input_schema.model_validate(call.arguments)
            validated.append((call.model_dump(), arguments))
        return validated

    async def dispatch(self, call, context: ToolContext):
        spec = self.tools[call['tool']]
        context.current()
        try:
            arguments = spec.input_schema.model_validate(call['arguments'])
        except ValueError:
            tool_activity(context, call, 'blocked', error_result('invalid_arguments', '工具参数无效，未执行工具'))
            raise
        if self.tool_guard is not None and not self.tool_guard(call, context):
            result = error_result('skill_tool_denied', '当前激活技能限制此工具，请先调整技能或加载允许工具')
            tool_activity(context, call, 'blocked', result)
            return result
        if spec.effect != 'read' and 'derive' not in context.grants:
            result = error_result('scope_denied', '当前范围不允许创建或更改成果')
            tool_activity(context, call, 'blocked', result)
            return result
        parent_id = getattr(arguments, 'parent_operation_id', None)
        if parent_id:
            parents = context.store.rows('SELECT o.id FROM operations o JOIN runs r ON r.id=o.run_id '
                'WHERE o.id=? AND r.session_id=?', (parent_id, context.session_id))
            if not parents:
                result = error_result('scope_denied', '原操作不属于当前会话')
                tool_activity(context, call, 'blocked', result)
                return result
        try:
            record, fresh = context.store.begin_operation(context.run_id, context.generation, call, parent_id)
        except RunSuperseded:
            raise
        except Conflict:
            tool_activity(context, call, 'blocked', error_result('revision_conflict', '调用身份与原参数不同，工具未执行'))
            raise
        context = replace(context, operation_id=record['id'])
        if not fresh and record['state'] == 'finished':
            result = json.loads(record['result'])
            tool_activity(context, call, 'restored', result)
            return result
        if not fresh and record['state'] == 'pending':
            result = json.loads(record['result'])
            tool_activity(context, call, 'restored', result)
            return result
        # An intent may have crossed a process boundary already. Only a native
        # request lookup or an idempotent business API makes replay permissible.
        if not fresh and spec.effect != 'read':
            recovered = await invoke(spec.lookup_operation, arguments, context) if spec.lookup_operation else None
            context.current()
            if recovered is not None:
                result = self.validated_result(spec, recovered, context)
                context.current()
                context.store.finish_operation(record['id'], result)
                tool_activity(context, call, 'restored', result)
                return result
            if not spec.idempotent:
                result = error_result('response_unknown', '原操作回执未知，需要核实后继续', status='needs_verification')
                context.current()
                context.store.finish_operation(record['id'], result)
                tool_activity(context, call, 'blocked', result)
                return result
        restored = False
        try:
            context.current()
            tool_activity(context, call, 'started')
            result = self.validated_result(spec, await invoke(spec.execute, arguments, context), context)
        except RunSuperseded:
            raise
        except Conflict:
            context.current()
            result = error_result('revision_conflict', '对象版本或操作身份已变化；请读取最新对象后使用新的调用身份修订')
        except (ValueError, KeyError) as caught:
            from workbench_platform.jobs.native_ocr import OcrUnavailable
            from workbench_platform.jobs.native_classification import ClassificationUnavailable
            from workbench_platform.application_services.errors import NativeReaderUnavailable
            if isinstance(caught, NativeReaderUnavailable):
                result = error_result('native_reader_unavailable', str(caught), data={'failure': {
                    'stage': 'native_reader_prepare', 'application': caught.application,
                    'cause_code': caught.cause_code, 'accepted_write': False,
                    'user_action': '原成果和固定版本已保留；本地服务恢复后继续原任务，未重新计算。'}})
            elif spec.effect != 'read' and isinstance(caught, (OcrUnavailable, ClassificationUnavailable)):
                result, restored = await self.recover_fault(spec, arguments, context)
            else:
                # Pydantic errors can include input bodies; project a stable error.
                result = input_failure(caught)
        except PermissionError:
            if spec.effect != 'read':
                result, restored = await self.recover_fault(spec, arguments, context)
            else:
                result = error_result('permission_denied', '访问被拒绝，请检查当前账号的资料和服务权限')
            result['data'].setdefault('failure', {}).update(
                cause_code='permission_denied', stage='access', attempt_id=context.operation_id,
                retryable=False, user_action='先核实原操作回执并修复访问权限，再明确继续。')
        except TimeoutError as caught:
            if spec.effect != 'read':
                result, restored = await self.recover_fault(spec, arguments, context)
            else:
                result = error_result('observation_timeout', '状态读取超时，请按原身份重新核实状态')
            stage = getattr(caught, 'failure_stage', 'observation' if spec.effect == 'read' else 'submission')
            if stage not in {'load', 'inference', 'result_commit', 'observation', 'submission'}:
                stage = 'observation' if spec.effect == 'read' else 'submission'
            result['data'].setdefault('failure', {}).update(
                cause_code=getattr(caught, 'error_code', 'observation_timeout' if spec.effect == 'read' else 'response_timeout'),
                stage=stage, attempt_id=context.operation_id, retryable=False,
                user_action='沿用原操作身份查回执；尚未核实的已接受操作不得自动重发。')
        except Exception:
            if spec.effect != 'read':
                result, restored = await self.recover_fault(spec, arguments, context)
            else:
                result = error_result('tool_failed', '工具执行未完成，请查看实际任务状态')
        context.current()
        context.store.finish_operation(record['id'], result)
        tool_activity(context, call, 'restored' if restored else 'pending' if result['status'] == 'pending' else 'finished', result)
        return result

    def validated_result(self, spec, value, context):
        result = spec.output_schema.model_validate(value).model_dump(mode='json')
        for ref in [*result['result_refs'], *result['job_refs'], *result['artifact_refs']]:
            if ref['workspace_id'] != context.workspace_id:
                raise ValueError('工具结果不属于当前项目')
        artifact_ids = {ref['object_id'] for ref in result['artifact_refs']}
        def attach_downloads(item):
            if isinstance(item, dict):
                if isinstance(item.get('bundle_id'), str) and item['bundle_id'] in artifact_ids and isinstance(item.get('files'), list):
                    for file in item['files']:
                        if isinstance(file, dict) and file.get('artifact_id') and isinstance(file.get('name'), str):
                            file['download_url'] = f'/api/platform/v1/workspaces/{context.workspace_id}/artifacts/{item["bundle_id"]}/files/' + quote(file['name'], safe='')
                for child in item.values():
                    attach_downloads(child)
            elif isinstance(item, list):
                for child in item:
                    attach_downloads(child)
        attach_downloads(result['data'])
        if len(canonical(result).encode('utf-8')) > 256 * 1024:
            raise ValueError('工具结果超过摘要上限，请分页读取')
        return result

    async def recover_fault(self, spec, arguments, context):
        # A transport/storage fault can occur after native success. Query the
        # same identity; never turn it into permission for a new submission.
        context.current()
        if spec.lookup_operation:
            try:
                recovered = await invoke(spec.lookup_operation, arguments, context)
                context.current()
                if recovered is not None:
                    return self.validated_result(spec, recovered, context), True
            except Conflict:
                raise
            except Exception:
                pass
        return error_result('response_unknown', '原操作回执未知，需要核实后继续', status='needs_verification',
            data={'failure': {'code': 'response_unknown', 'stage': 'receipt_lookup', 'attempt_id': context.operation_id,
                             'accepted_write': None, 'retryable': False,
                             'user_action': '沿用原身份查询原生回执；回执未知时保留待核实，禁止自动创建新请求。'}}), False

    def as_langchain_tool(self, name, context: ToolContext):
        """A StructuredTool uses the same strict schema and actual dispatcher."""
        from langchain_core.tools import StructuredTool
        spec = self.tools[name]

        async def execute(**arguments):
            return await self.dispatch({'call_id': context.operation_id, 'tool': name,
                                        'arguments': arguments}, context)

        return StructuredTool.from_function(coroutine=execute, name=name,
                    description=spec.description, args_schema=spec.input_schema, infer_schema=False)
