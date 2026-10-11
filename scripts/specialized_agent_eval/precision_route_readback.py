"""Independent full-attempt and original-budget audit for optional routed runs."""
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from common import fingerprint


def _box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        box = [float(number) for number in value]
    except (TypeError, ValueError):
        return None
    return box if all(math.isfinite(n) for n in box) and box[2] > box[0] and box[3] > box[1] else None


def expected_route(info, evidence, source_hash, version):
    """Recompute the published rule without importing any production router."""
    source = Path(info['input_path']).resolve()
    if (source.drive.upper() != 'F:' or info['input_sha256'] != source_hash
            or hashlib.sha256(source.read_bytes()).hexdigest() != source_hash):
        raise ValueError('Route source bytes differ')
    regions = [box for region in info.get('business_regions', [])
        if isinstance(region, dict) and (box := _box(region.get('table_box')))]
    provenance = evidence.get('provenance', {})
    trusted = (provenance.get('contains_reference_input') is False
        and provenance.get('image_version') == version
        and provenance.get('input_image_version') == version
        and provenance.get('source_sha256') == source_hash
        and provenance.get('coordinate_space') == 'exif_normalized_original'
        and provenance.get('source_candidate_id', 'original') == 'original')
    selected = []
    for reading in evidence.get('text', []):
        if (not trusted or not isinstance(reading, dict)
                or not str(reading.get('source', '')).startswith('PP-OCR')
                or reading.get('kind') in {'toolbar', 'taskbar', 'formula_bar', 'ruler', 'window_border', 'scrollbar', 'ui'}):
            continue
        box = _box(reading.get('box', reading.get('bbox', reading.get('coordinate'))))
        if box is None:
            polygon = reading.get('polygon')
            if polygon:
                box = _box([min(point[0] for point in polygon), min(point[1] for point in polygon),
                    max(point[0] for point in polygon), max(point[1] for point in polygon)])
        if box is None:
            continue
        for region in regions:
            intersection = max(0, min(box[2], region[2]) - max(box[0], region[0])) * max(
                0, min(box[3], region[3]) - max(box[1], region[1]))
            if intersection >= .5 * (box[2] - box[0]) * (box[3] - box[1]):
                selected.append(reading)
                break
    content = ' '.join(str(reading.get('text', '')) for reading in selected)
    han = sum('\u3400' <= char <= '\u9fff' for char in content)
    latin = sum(char.isascii() and char.isalpha() for char in content)
    engines, reason = ['hunyuan'], 'source_language_unverified_or_undecided'
    if trusted and han >= 20 and han > latin:
        reason = 'cjk_business_region'
    elif trusted and latin >= 20 and latin >= 4 * max(1, han):
        engines, reason = ['glm', 'hunyuan'], 'latin_business_region'
    return {'version': 'screen-business-language-route-v1', 'source_sha256': source_hash,
        'input_version_id': version, 'source_regions_sha256': fingerprint(regions),
        'source_evidence_sha256': fingerprint(evidence), 'engines': engines, 'reason': reason,
        'han_count': han, 'ascii_latin_count': latin, 'matched_auxiliary_readings': len(selected),
        'matched_text_sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
        'candidate_values_read': False, 'reference_input_used': False, 'human_review_required': True}


def _cost(task, link):
    cost = float(link.get('spent_seconds') or 0)
    if task.get('started'):
        if not task.get('finished'):
            raise ValueError('Terminal attempted task lacks its finished timestamp')
        cost += max(0, (datetime.fromisoformat(task['finished']) - datetime.fromisoformat(task['started'])).total_seconds())
    if not math.isfinite(cost) or cost < 0:
        raise ValueError('Invalid preserved task cost')
    return cost


def verify_routed_ledger(policy, items, tasks, contexts, screen_tasks):
    """Keep every attempt, including failed primaries and successful fallbacks."""
    task_by_id = {task['id']: task for task in tasks}
    if len(task_by_id) != len(tasks) or len({link['task_id'] for link in screen_tasks}) != len(screen_tasks):
        raise ValueError('Duplicate task identity')
    by_input, contexts_by_input = defaultdict(list), defaultdict(list)
    for link in screen_tasks:
        by_input[link['input_version_id']].append(link)
    for link in contexts:
        contexts_by_input[link['input_version_id']].append(link)
    if set(by_input) - set(items) or set(contexts_by_input) != set(items):
        raise ValueError('Attempt ledger has added inputs or missing source contexts')
    records, models, per_source = [], [], defaultdict(float)
    for version, item in items.items():
        info = json.loads(item['preparation']) if item.get('preparation') else {}
        evidence = json.loads(item['evidence']) if item.get('evidence') else {}
        route = expected_route(info, evidence, item['source_hash'], version)
        if info.get('engine_route_snapshot') != route:
            raise ValueError('Source-recomputed route snapshot differs')
        links = sorted(by_input[version], key=lambda link: (link['pass_index'], link['prepared_id']))
        identities, attempts = set(), []
        for link in links:
            task = task_by_id[link['task_id']]
            metadata = json.loads(link['metadata'])
            index = link['pass_index']
            identity = (index, link['prepared_id'], link['attempt'])
            if identity in identities:
                raise ValueError('Duplicate prepared-image attempt')
            identities.add(identity)
            reserve = policy['technical_fallback_reserve_seconds'] if index == 0 and len(route['engines']) == 2 else 0
            if (not 0 <= index < len(route['engines']) or task['engine'] != route['engines'][index]
                    or task['kind'] not in {'ocr', 'screen_cached'}
                    or task['status'] not in {'succeeded', 'failed', 'cancelled'}
                    or metadata.get('engine_route_snapshot') != route
                    or metadata.get('technical_fallback_reserved_seconds') != reserve
                    or metadata.get('input_version_id') != version
                    or link['attempt'] != 0):
                raise ValueError('Attempt engine, source, reserve or terminal identity differs')
            timeout = metadata['timeout_seconds']
            if type(timeout) is not int or not 0 <= timeout <= policy['budget_seconds'] - reserve:
                raise ValueError('Attempt exceeds its frozen primary envelope')
            models.append(task)
            attempts.append({'task_id': task['id'], 'engine': task['engine'], 'pass_index': index,
                'prepared_id': link['prepared_id'], 'status': task['status'], 'kind': task['kind'],
                'started': task.get('started'), 'finished': task.get('finished'),
                'result_id': task.get('result_id'), 'carried_seconds': link.get('spent_seconds', 0),
                'frozen_timeout_seconds': timeout, 'technical_fallback_reserved_seconds': reserve,
                'actual_seconds': _cost(task, link), 'error': task.get('error')})
        if any(attempt['pass_index'] == 1 for attempt in attempts) and any(
                attempt['pass_index'] == 0 and attempt['status'] == 'succeeded' for attempt in attempts):
            raise ValueError('Successful primary unexpectedly triggered a second model')
        if any(attempt['pass_index'] == 1 for attempt in attempts) and not any(
                attempt['pass_index'] == 0 for attempt in attempts):
            skipped = info.get('engine_route_primary_skip') or {}
            if (skipped.get('reason') != 'primary_envelope_exhausted'
                    or skipped.get('model_invoked') is not False
                    or skipped.get('fallback_reserve_seconds') != policy['technical_fallback_reserve_seconds']
                    or not 1 <= skipped.get('original_remaining_seconds', 0) < policy['technical_fallback_reserve_seconds'] + 1):
                raise ValueError('Missing failed primary without an explicit budget skip receipt')
        if not attempts and item['status'] not in {'failed', 'cancelled'}:
            raise ValueError('Usable output lacks actual attempted tasks')
        context_cost = 0
        for link in contexts_by_input[version]:
            task = task_by_id[link['task_id']]
            if task['kind'] not in {'screen_context', 'screen_context_cached'} or task['status'] not in {'succeeded', 'failed', 'cancelled'}:
                raise ValueError('Source context is not terminal')
            context_cost += _cost(task, link)
        preparation_cost = float(info.get('preparation_seconds', 0))
        if not math.isfinite(preparation_cost) or preparation_cost < 0:
            raise ValueError('Invalid preparation cost')
        spent = context_cost + preparation_cost + sum(attempt['actual_seconds'] for attempt in attempts)
        per_source[item['source_hash']] += spent
        records.append({'input_version_id': version, 'source_sha256': item['source_hash'],
            'route': route, 'context_seconds': context_cost, 'preparation_seconds': preparation_cost,
            'primary_budget_skip': info.get('engine_route_primary_skip'),
            'attempts': attempts, 'actual_seconds': spent,
            'original_remaining_seconds': max(0, policy['budget_seconds'] - spent)})
    return models, {'format': 'independent-routed-attempt-ledger-v1',
        'inputs': len(items), 'model_attempts': len(models), 'records': records,
        'attempt_states': dict(Counter(task['status'] for task in models)),
        'engines': dict(Counter(task['engine'] for task in models)),
        'per_source_actual_seconds': dict(per_source),
        'source_budget_overshoots': {key: spent - policy['budget_seconds'] for key, spent in per_source.items() if spent > policy['budget_seconds']},
        'root_budget_overshoot_seconds': max(0, sum(per_source.values()) - policy['batch_budget_seconds']),
        'failed_attempts_retained': True, 'budgets_extended': False,
        'production_route_imported': False, 'truth_read': False, 'final_agent_qualification': False}
