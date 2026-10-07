"""Estimate only the known resident stage from this batch's actual work."""
from datetime import datetime, timezone
import math


PENDING = {'queued', 'running'}
CACHED = {'screen_cached', 'screen_context_cached', 'screen_final'}
ENGINE_NAMES = {'hunyuan': 'Hunyuan', 'glm': 'GLM', 'paddlevl': 'PaddleOCR-VL'}


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.timestamp()
    except (ValueError, OverflowError):
        return None


def _duration(row, current_time=None):
    start = _timestamp(row.get('started'))
    finish = _timestamp(row.get('finished')) if current_time is None else current_time
    prior = row.get('spent_seconds', 0)
    if (start is None or finish is None or not isinstance(prior, (int, float))
            or not math.isfinite(prior) or prior < 0 or finish < start):
        return None
    duration = prior + finish - start
    return duration if math.isfinite(duration) else None


def _phase(row):
    if row.get('kind') == 'screen_context':
        return ('context', 'geometry', -1)
    return ('ocr', row.get('engine'), row.get('pass_index', 0))


def _quantile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low, high = int(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def estimate_screen_eta(rows, *, batch_status='running', pending_images=0, timestamp=None):
    """No extrapolation to future routes, caches, exports or human review.

    Rows contain task timestamps and persisted active-segment seconds. They must
    come from this batch. At least three successful samples after the first
    completed task of the same stage/engine are required. The first task can
    include cold loading; its cost is not multiplied over the remaining queue.
    """
    timestamp = datetime.now(timezone.utc).timestamp() if timestamp is None else timestamp
    result = {'remaining_seconds': None, 'stage': None, 'sample_count': 0,
              'pending_tasks': 0, 'cached_pending_tasks': 0,
              'scope': 'current_queued_stage', 'pending_images': pending_images,
              'not_included': ['future_preprocessing', 'future_upgrades',
                               'model_switches', 'export', 'human_review'],
              'basis': 'same_batch_successful_task_active_wall_time_p75',
              'reason': 'insufficient_samples'}
    if batch_status != 'running':
        result['reason'] = 'paused' if batch_status == 'paused' else 'not_running'
        return result
    pending = [row for row in rows if row.get('status') in PENDING and row.get('kind') not in CACHED]
    result['cached_pending_tasks'] = sum(row.get('status') in PENDING and row.get('kind') in CACHED for row in rows)
    if not pending:
        result['reason'] = 'no_queued_compute'
        return result
    running = [row for row in pending if row.get('status') == 'running']
    anchor = min(running or pending, key=lambda row: (_phase(row)[2], row.get('created', ''), row.get('id', '')))
    phase = _phase(anchor)
    current = [row for row in pending if _phase(row) == phase]
    result.update(stage={'kind': phase[0], 'engine': phase[1], 'pass_index': phase[2],
                         'label': '定位业务表区' if phase[0] == 'context' else
                                  ENGINE_NAMES.get(phase[1], phase[1]) + ('首轮识别' if phase[2] == 0 else '精度升级')},
                  pending_tasks=len(current))
    completed = sorted((row for row in rows if row.get('status') == 'succeeded'
                        and row.get('kind') not in CACHED and _phase(row) == phase
                        and _duration(row) is not None), key=lambda row: (row.get('started', ''), row.get('id', '')))
    # Require three warm observations after the first potentially cold task.
    durations = [_duration(row) for row in completed[1:]]
    result['sample_count'] = len(durations)
    if len(durations) < 3:
        return result
    typical = _quantile(durations, .75)
    remaining = 0.0
    for row in current:
        elapsed = _duration(row, timestamp) if row.get('status') == 'running' else 0
        if row.get('status') == 'running' and elapsed is None:
            result['reason'] = 'invalid_active_timestamp'
            return result
        # Once a live task exceeds the observed percentile, its completion is
        # unknown; reporting zero for it would falsely imply an imminent finish.
        if row.get('status') == 'running' and elapsed >= typical:
            result['reason'] = 'active_task_exceeds_observed_duration'
            return result
        remaining += max(0, typical - elapsed)
    result.update(remaining_seconds=math.ceil(remaining), reason='estimated')
    return result
