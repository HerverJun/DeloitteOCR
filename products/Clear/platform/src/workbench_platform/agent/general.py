"""Natural turn completion for new sessions; legacy task receipts stay readable."""
from __future__ import annotations

import json

from .store import digest

CONTRACT = 'clear-general-agent-v1'
METADATA_TOOLS = {'audit_update_plan', 'skills_activate', 'skills_deactivate', 'tools_describe', 'tools_search'}


def enabled(state):
    return state.get('execution_contract') == CONTRACT


async def finish_text(runtime, state):
    response = state.get('final_response')
    if not enabled(state) or not response:
        return False
    content = response['content']
    from .workspace_outputs import automatic
    outputs = await automatic(runtime, state, response['request_id'])
    from .general_outputs import remaining_items, verify_outputs, output_links
    content = output_links(content, outputs, state['workspace_id'])
    references = {}
    results = []
    for record in runtime.store.rows('SELECT result FROM operations WHERE run_id=? AND parent_id IS NULL AND result IS NOT NULL ORDER BY created',
                                     (state['run_id'],)):
        result = json.loads(record['result'])
        results.append(result)
        if result.get('status') != 'success':
            continue
        for ref in [*result.get('result_refs', []), *result.get('artifact_refs', [])]:
            if ref.get('kind') in {'result', 'table', 'artifact', 'evidence'}:
                references[digest(ref)] = ref
    delivery = outputs if outputs and outputs['status'] != 'success' else state.get('delivery')
    remaining = remaining_items(results)
    if references:
        policy = runtime.tasks.get(state['session_id']).get('delivery_policy_resolution', {})
        if policy.get('require_review'):
            call = {'call_id': 'auto_output_' + digest(response['request_id'])[:32],
                    'tool': 'delivery_publish', 'arguments': {
                        'summary': content[:2000], 'artifact_refs': [ref for ref in references.values() if ref['kind'] == 'artifact'],
                        'result_refs': [ref for ref in references.values() if ref['kind'] != 'artifact']}}
            delivery = await runtime.registry.dispatch(call, runtime.context(state))
            if delivery.get('status') == 'needs_user':
                records = runtime.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?',
                    (state['run_id'], call['call_id']))
                settled = await runtime.harness_settle(state, call, delivery, records[0]['id'])
                state['completion_review_notice'] = {
                    'decision_id': delivery['data']['decision_id'], 'reply': settled['data']['reply'],
                    'action': delivery['data'].get('action'), 'summary': delivery['summary']}
                state.pop('final_response', None)
                state['delivery'] = None
                runtime.graph.save(state)
                return False
        elif not (delivery or {}).get('data', {}).get('verified'):
            delivery = await verify_outputs(runtime, state, list(references.values()), content)
        state['delivery'] = delivery
    await runtime.collect_available_results(state['run_id'])
    verified = (delivery or {}).get('data', {}).get('verified') is True
    incomplete = bool(remaining) or bool(delivery) and (not verified or delivery.get('data', {}).get('completeness') == 'partial')
    if incomplete:
        # Keep the same DSH turn alive for bounded repair of missing outputs.
        # Review denials and unknown effects remain genuine blockers.
        from .goal_progress import stable
        details = {'failures': remaining, 'coverage': (delivery or {}).get('data', {}).get('requirement_coverage', {}),
                   'limitations': (delivery or {}).get('data', {}).get('limitations', [])}
        error_code = (delivery or {}).get('error', {}).get('code')
        repairable = bool(remaining or verified) and error_code not in {'review_required', 'response_unknown'}
        signature = digest(stable(details))
        attempts = state.get('output_repair_attempts', 0) + 1 if signature == state.get('output_repair_signature') else 1
        if repairable and attempts <= 2 and not state.get('completion_review_notice'):
            state.update(output_repair_signature=signature, output_repair_attempts=attempts,
                         remaining_delivery_work={**details, 'instruction': '已有成果可读取下载；只补齐失败或缺失项，复用原回执，不重做成功项。可用自然文字完成。'},
                         delivery=None)
            state.pop('final_response', None)
            runtime.graph.save(state)
            return False
        reason = ('已有输出已保存，仍有' + str(len(remaining)) + '项未完成') if remaining else delivery.get('summary', '已有输出已保存，部分要求仍待完成')
        status = 'paused'
        content += '\n\n' + reason
    else:
        state.pop('remaining_delivery_work', None)
        state.pop('output_repair_signature', None)
        state.pop('output_repair_attempts', None)
        status = 'succeeded' if state.get('business_started') or references else 'answered'
    display_id = response['request_id'] + ':text'
    refs = list(references.values())
    runtime.store.response_state(state['run_id'], state['generation'], response['request_id'], display_id,
        'confirmed', content=content, source='text', references=refs)
    payload = {'role': 'assistant', 'content': content, 'references': refs,
               'response_type': 'execution' if status != 'answered' else 'discussion', 'display_id': display_id}
    runtime.event(state, response['request_id'] + ':final-text', 'message', payload)
    if verified:
        runtime.event(state, response['request_id'] + ':outputs', 'delivery', {
            'status': 'partial' if incomplete else 'succeeded', 'content': content,
            'delivery': delivery, 'display_id': display_id})
    if incomplete:
        runtime.store.status(state['run_id'], state['generation'], status, reason)
    else:
        runtime.status(state, status)
    runtime.close_presentations(state, status)
    runtime.graph.save(state)
    return True
