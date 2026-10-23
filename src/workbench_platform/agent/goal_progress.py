"""Progress belongs to business evidence, not tool names or retry arguments."""
from .store import digest


REPEAT_WARNING_AT = 3
REPEAT_STOP_AT = 4
CONTENT_KEYS = {'text', 'full_text', 'content', 'rows', 'tables', 'predictions', 'result_id',
                'result_hash', 'sha256', 'model_id', 'value', 'values', 'amount', 'label', 'labels'}
DEPENDENCY_KEYS = {'handoff_id', 'binding_id', 'asset_set_id', 'data_session_id', 'dataset_id',
                   'document_id', 'link_id', 'project_id', 'batch_id', 'version'}
VOLATILE_KEYS = {'updated_at', 'created_at', 'sampled_at', 'timestamp', 'elapsed', 'duration',
                 'request_id', 'call_id', 'operation_id', 'summary', 'message'}


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [stable(item) for item in value]
    return value


def content(data):
    output = {key: stable(value) for key, value in data.items() if key in CONTENT_KEYS | DEPENDENCY_KEYS}
    if isinstance(data.get('items'), list):
        items = [content(item) for item in data['items'] if isinstance(item, dict)]
        if any(items):
            output['items'] = items
    if output:
        # Equal values on different real pages still cover different input;
        # an empty page or a changed request offset alone proves nothing.
        output.update({key: data[key] for key in ('offset', 'text_offset', 'page_number') if key in data})
    return output


def observe(state, call, result):
    seen = set(state.get('progress_evidence', []))
    evidence = []
    action = digest([call.get('tool'), stable(call.get('arguments', {}))])
    failed = dict(state.get('progress_failed_actions', {}))
    if result['status'] == 'success':
        for key in ('result_refs', 'artifact_refs'):
            evidence.extend(digest([key, stable(ref)]) for ref in result.get(key, []))
        data = content(result.get('data') or {})
        if data:
            evidence.append(digest(['content', data]))
        coverage = result.get('coverage') or {}
        completed = coverage.get('succeeded', (coverage.get('counts') or {}).get('succeeded', 0))
        if isinstance(completed, int) and completed > state.get('progress_coverage', 0):
            state['progress_coverage'] = completed
            evidence.append(digest(['coverage', completed]))
        checks = (result.get('data') or {}).get('delivery_checks') or []
        evidence.extend(digest(['check', stable(check)]) for check in checks
                        if check.get('passed') is True or check.get('status') == 'passed')
        if action in failed:
            evidence.append(digest(['resolved', action, failed.pop(action)]))
    new = [item for item in evidence if item not in seen]
    if result['status'] not in {'pending', 'needs_user', 'needs_verification'}:
        signature = digest([action, stable(result)])
        state['repeated_action'] = state.get('repeated_action', 0) + 1 if signature == state.get('last_action') else 1
        state['last_action'] = signature
    if new:
        state['progress_evidence'] = list(dict.fromkeys([*state.get('progress_evidence', []), *new]))[-512:]
        state['no_progress'] = 0
        state.pop('progress_repair', None)
    elif result['status'] not in {'pending', 'needs_user', 'needs_verification'}:
        state['no_progress'] = state.get('no_progress', 0) + 1
    if result.get('error'):
        error = result['error']
        state['progress_blocker'] = {'code': error.get('code', 'tool_error'),
                                     'message': error.get('message') or result['summary']}
        failed[action] = error.get('code', 'tool_error')
    state['progress_failed_actions'] = dict(list(failed.items())[-128:])
    if state.get('repeated_action', 0) >= REPEAT_WARNING_AT:
        # A bounded repair opportunity is delivered to the controller in the
        # normal receipt; accepted writes are never automatically resubmitted.
        state['progress_repair'] = True
        result = {**result, 'data': {**(result.get('data') or {}), 'continuation_guard': {
            'remaining_repeated_attempts': max(0, REPEAT_STOP_AT - state['repeated_action']),
            'blocker': state.get('progress_blocker'),
            'instruction': '相同参数已连续得到相同结果三次。请改变处理方式或说明无法继续的原因；再次重复此调用将暂停。已有回执和成果可直接复用。'}}}
    return result


def reset_for_input(state, messages):
    identifiers = set(state.get('progress_inputs', []))
    incoming = {str(item.get('id') or digest(item)) for item in messages if item.get('role') == 'user'}
    if incoming - identifiers:
        state.update(no_progress=0, repeated_action=0, progress_inputs=sorted(identifiers | incoming))
        state.pop('progress_repair', None)
