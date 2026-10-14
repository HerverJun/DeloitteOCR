"""Strict executable tool registry with server-only context and operation IDs."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import asyncio
import inspect
import json
from typing import Callable, Literal

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


@dataclass(frozen=True)
class ToolContext:
    workspace_id: str
    session_id: str
    run_id: str
    generation: int
    turn_id: int
    operation_id: str
    selected_assets: tuple[str, ...]
    services: object
    store: object
    grants: frozenset[str] = frozenset({'read', 'derive'})

    def current(self):
        self.store.assert_current(self.run_id, self.generation)
        workspace = self.services.workspace_store.get_workspace(self.workspace_id)
        if workspace is None:
            raise ValueError('项目已不存在')


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


async def invoke(handler, *arguments):
    if inspect.iscoroutinefunction(handler):
        return await handler(*arguments)
    result = await asyncio.to_thread(handler, *arguments)
    return await result if inspect.isawaitable(result) else result


def error_result(code, message, *, status='error'):
    return AgentToolResult(status=status, summary=message,
                           error={'code': code, 'message': message, 'retryable': False}).model_dump(mode='json')


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
        self.tool_guard = None

    def register(self, specification: ToolSpec):
        AgentCall(call_id='registration', tool=specification.name, arguments={})
        if specification.name in self.tools or not callable(specification.execute):
            raise ValueError('Duplicate tool or missing handler')
        if specification.input_schema.model_config.get('extra') != 'forbid':
            raise ValueError('Tool input must forbid extra parameters')
        self.tools[specification.name] = specification

    def describe(self, name, services=None):
        spec = self.tools[name]
        availability = spec.availability(services) if spec.availability and services else {'state': 'ready'}
        return {'name': name, 'version': spec.version, 'description': spec.description,
                'input_schema': spec.input_schema.model_json_schema(),
                'output_schema': spec.output_schema.model_json_schema(),
                'effect': spec.effect, 'input_kinds': spec.input_kinds, 'output_kinds': spec.output_kinds,
                'examples': spec.examples, 'limits': spec.limits, 'availability': availability,
                'has_handler': True, 'has_lookup': spec.lookup_operation is not None}

    def schemas(self, visible):
        return {name: {**compact_schema(self.tools[name].input_schema.model_json_schema()),
                       'description': self.tools[name].description} for name in visible if name in self.tools}

    def catalog(self, *, keyword='', family='', offset=0, limit=20, services=None):
        """A discovery page is metadata only; it grants no executable schema."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 30:
            raise ValueError('工具目录分页无效')
        needle = keyword.casefold()
        names = [name for name, spec in self.tools.items()
                 if (not family or name.split('_', 1)[0] == family)
                 and (not needle or needle in name.casefold() or needle in spec.description.casefold())]
        items = []
        for name in names[offset:offset + limit]:
            spec = self.tools[name]
            availability = spec.availability(services) if spec.availability and services else {'state': 'ready'}
            items.append({'name': name, 'description': spec.description[:240],
                          'family': name.split('_', 1)[0], 'effect': spec.effect,
                          'availability': availability})
        return {'tools': items, 'total': len(names), 'offset': offset,
                'next_offset': offset + limit if offset + limit < len(names) else None,
                'families': sorted({name.split('_', 1)[0] for name in self.tools})}

    def active_tools(self, visible, *, core=(), protected=()):
        """Stable activation order allows oldest unrelated schemas to be reclaimed."""
        names = sorted(visible) if isinstance(visible, (set, frozenset)) else list(visible)
        return list(dict.fromkeys(name for name in [*core, *names, *protected] if name in self.tools))

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
            if spec.effect != 'read' and isinstance(caught, (OcrUnavailable, ClassificationUnavailable)):
                result, restored = await self.recover_fault(spec, arguments, context)
            else:
                # Pydantic errors can include input bodies; project a stable error.
                result = error_result('invalid_input', '工具参数、版本或资料范围无效，请重新查询并修正')
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
        if len(canonical(result).encode('utf-8')) > 256 * 1024:
            raise ValueError('工具结果超过摘要上限，请分页读取')
        for ref in [*result['result_refs'], *result['job_refs'], *result['artifact_refs']]:
            if ref['workspace_id'] != context.workspace_id:
                raise ValueError('工具结果不属于当前项目')
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
        return error_result('response_unknown', '原操作回执未知，需要核实后继续', status='needs_verification'), False

    def as_langchain_tool(self, name, context: ToolContext):
        """A StructuredTool uses the same strict schema and actual dispatcher."""
        from langchain_core.tools import StructuredTool
        spec = self.tools[name]

        async def execute(**arguments):
            return await self.dispatch({'call_id': context.operation_id, 'tool': name,
                                        'arguments': arguments}, context)

        return StructuredTool.from_function(coroutine=execute, name=name,
                    description=spec.description, args_schema=spec.input_schema, infer_schema=False)
