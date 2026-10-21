"""Shared execution and event adapters; no model or Harness dependencies."""
import asyncio
import inspect
from .identity import digest


async def invoke(handler, *arguments):
    if inspect.iscoroutinefunction(handler):
        return await handler(*arguments)
    result = await asyncio.to_thread(handler, *arguments)
    return await result if inspect.isawaitable(result) else result


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
