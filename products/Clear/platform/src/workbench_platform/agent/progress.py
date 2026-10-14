"""Bounded public observations of the existing native job observer.

No timer, model call, submission or synthetic percent lives here. Counts and
stage units are reported only when the native family actually supplies them.
"""
from __future__ import annotations

from .store import RunSuperseded


def _counts(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key, number in value.items()
            if isinstance(key, str) and (number is None or type(number) is int and number >= 0)}


def progress_for(reference, context):
    """Read one exact, session-owned job using existing native family readers."""
    from .tools.jobs import owned, read_reference
    from workbench_platform.application_services.job_lifecycle import adapter

    context.current()
    ref = reference.model_dump(mode='json') if hasattr(reference, 'model_dump') else reference
    if not isinstance(ref, dict) or ref.get('kind') != 'job' or ref.get('workspace_id') != context.workspace_id:
        raise ValueError('进度任务引用不属于当前项目')
    _, recorded = owned(context, ref['object_id'])
    identity = ('kind', 'workspace_id', 'object_id', 'application', 'parent_id', 'native_id')
    if (any(ref.get(key) != recorded.get(key) for key in identity)
            or ref.get('coverage', {}).get('kind') != recorded.get('coverage', {}).get('kind')):
        raise ValueError('进度引用与原任务身份不一致')
    ref = recorded
    family = ref['application']
    kind = ref.get('coverage', {}).get('kind', family)
    public = {'version': 1, 'generation': context.generation, 'application': family,
              'job_id': ref['object_id'], 'native_kind': kind, 'status': 'unknown',
              'stage': '', 'counts': {}, 'capabilities': [], 'observation': 'native'}
    try:
        value = read_reference(ref, context)
        public['status'] = str(value.get('status', value.get('state', 'unknown')))[:80]
        public['stage'] = str(value.get('stage', value.get('action', '')))[:256]
        public['counts'] = _counts(value.get('counts'))
        public['capabilities'] = [item for item in value.get('capabilities', [])
                                  if item in {'pause', 'resume', 'cancel', 'retry_failed'}]
        public['has_error'] = bool(value.get('error') or value.get('native_errors'))
        if 'availability' in value:
            public['availability'] = str(value['availability'])[:80]
            if value['availability'] not in {'available', 'ready'}:
                public['observation'] = 'unavailable'
        for field in ('progress_done', 'progress_total'):
            number = value.get(field)
            if number is None or type(number) is int and number >= 0:
                public[field] = number
        public.update(adapter(ref).progress(value))
    except RunSuperseded:
        raise
    except Exception:
        # The identity was checked before the read. A failed observation is not
        # proof that a native job failed, succeeded or stopped.
        public.update(status='unknown', stage='', counts={}, capabilities=[],
                      observation='unavailable', availability='unreachable', has_error=False)
    context.current()
    return public
