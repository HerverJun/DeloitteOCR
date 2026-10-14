"""One dynamic LangGraph. Business tool choices belong to the controller."""
import json

from .dependencies import load_dependencies
from .registry import invoke, error_result, tool_activity
from dataclasses import replace
from .state import AgentState


def build_graph(runtime, saver):
    StateGraph, _ = load_dependencies()
    from langgraph.graph import START, END
    from langgraph.types import interrupt

    async def receive_input(state):
        runtime.current(state)
        changed_scope = runtime.tasks.apply_scope(state['session_id'])
        additions, identifiers = runtime.inbox(state)
        if changed_scope:
            additions.append({'id': state['run_id'] + ':scope:' + str(changed_scope['scope_revision']),
                              'role': 'user', 'content': '[服务器已应用用户明确变更的资料范围]\n' + json.dumps(changed_scope, ensure_ascii=False)})
        return {'messages': additions, 'inbox_received': identifiers,
                **({'delivery': None, 'answer': None, 'delivery_display_id': None, 'answer_display_id': None} if additions else {})}

    async def model(state):
        runtime.current(state)
        limits = runtime.current(state)
        if state['step'] >= limits['round_limit'] or state['tool_count'] >= limits['tool_limit']:
            return {'wait': 'budget', 'final_text': '本轮已达到配置的模型或工具预算，已有成果保留。'}
        response = await runtime.model(state)
        runtime.current(state)
        # Provider protocol text is a generated process note, not a confirmed
        # user answer. The runtime's public projection owns its streamed view.
        runtime.activity(state, 'validating_calls')
        return {'messages': [response], 'calls': response['calls'], 'cursor': 0, 'results': [],
                'visible': response.get('selected_tools', state['visible']), 'protected_tools': [],
                'wait': None, 'decision_id': None, 'final_text': response['content']}

    def after_model(state):
        return 'await_user' if state.get('wait') == 'budget' else 'validate_calls' if state['calls'] else 'finalize'

    async def validate_calls(state):
        try:
            runtime.registry.validate_batch(state['calls'], set(state['visible']))
            if state['tool_count'] + len(state['calls']) > runtime.current(state)['tool_limit']:
                raise ValueError('工具预算不足以执行完整批次')
        except ValueError:
            count = state.get('correction_count', 0) + 1
            results = [error_result('invalid_arguments', '整批工具参数或范围无效，未执行任何工具；请按真实schema修正')
                       for _ in state['calls']]
            for call, result in zip(state['calls'], results, strict=True):
                tool_activity(runtime.context(state), call, 'blocked', result)
            return {'results': results, 'cursor': len(state['calls']), 'correction_count': count,
                    'wait': 'error_limit' if count > 2 else None}
        return {'correction_count': 0}

    def after_validation(state):
        return 'collect_results' if state['cursor'] == len(state['calls']) else 'dispatch'

    async def dispatch(state):
        runtime.current(state)
        additions, _ = runtime.inbox(state)
        if additions:
            # Pair every withheld call before returning to the model; no stale
            # batch write starts after an additional user goal has arrived.
            withheld = [error_result('superseded', '收到追加目标，此调用尚未执行；先读取新目标再选择下一步')
                        for _ in state['calls'][state['cursor']:]]
            for call, result in zip(state['calls'][state['cursor']:], withheld, strict=True):
                tool_activity(runtime.context(state), call, 'blocked', result)
            return {'results': [*state['results'], *withheld], 'cursor': len(state['calls']) - 1}
        call = state['calls'][state['cursor']]
        runtime.activity(state, 'tool_dispatch', tool=call['tool'], call_id=call['call_id'])
        result = await runtime.registry.dispatch(call, runtime.context(state))
        runtime.event(state, call['call_id'] + ':submitted', 'tool_step', {'tool': call['tool'], 'call_id': call['call_id'],
            'summary': result['summary'], 'status': result['status'], 'job_refs': result['job_refs']})
        return {'results': [*state['results'], result], 'tool_count': state['tool_count'] + 1}

    def after_dispatch(state):
        result = state['results'][-1]
        if result['status'] == 'pending':
            return 'await_jobs'
        if result['status'] in {'needs_user', 'needs_verification'}:
            return 'await_user'
        return 'advance'

    async def await_jobs(state):
        runtime.current(state)
        result = state['results'][-1]
        settled = await runtime.settle(result, state)
        if settled is None:
            runtime.status(state, 'waiting_job')
            runtime.activity(state, 'waiting_job', job_refs=result['job_refs'])
            interrupt({'kind': 'native_jobs', 'job_refs': result['job_refs']})
            # The node rechecks authoritative job state when resumed, never
            # accepts a browser-supplied tool result as a native receipt.
            settled = await runtime.settle(result, state)
            if settled is None:
                raise ValueError('Native job observer resumed before settlement')
        runtime.current(state)
        call = state['calls'][state['cursor']]
        record = runtime.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?',
                                    (state['run_id'], call['call_id']))[0]
        runtime.store.finish_operation(record['id'], settled)
        tool_activity(replace(runtime.context(state), operation_id=record['id']), call, 'finished', settled)
        runtime.event(state, call['call_id'] + ':finished', 'tool_step', {'tool': call['tool'], 'call_id': call['call_id'],
            'status': settled['status'], 'summary': settled['summary'], 'artifact_refs': settled['artifact_refs'],
            'coverage': settled['coverage']})
        runtime.status(state, 'running')
        return {'results': [*state['results'][:-1], settled]}

    async def await_user(state):
        runtime.current(state)
        result = state['results'][-1] if state.get('results') else {}
        if state.get('wait') == 'budget':
            decision = runtime.budget_decision(state)
        else:
            decision = result.get('data', {}).get('decision_id')
        if not decision:
            runtime.status(state, 'needs_verification', '原操作回执未知，需核实')
            runtime.close_presentations(state, 'needs_verification', '原操作回执未知，需核实')
            interrupt({'kind': 'verification', 'message': result.get('summary', '')})
            raise ValueError('Unknown operation needs authoritative reconciliation')
        runtime.status(state, 'waiting_user')
        runtime.activity(state, 'waiting_user')
        question = runtime.store.rows('SELECT question,kind FROM decisions WHERE id=?', (decision,))[0]
        runtime.event(state, decision + ':question', 'question', {'decision_id': decision, 'revision': 1,
            'question': question['question'],
            'options': ['继续（增加20轮、60次工具）', '结束本轮'] if question['kind'] == 'budget' else result.get('data', {}).get('options', [])})
        interrupt({'kind': 'question', 'decision_id': decision})
        reply = runtime.store.rows('SELECT reply FROM decisions WHERE id=? AND run_id=?', (decision, state['run_id']))
        if not reply or not reply[0]['reply']:
            raise ValueError('Question has no bound reply')
        runtime.status(state, 'running')
        if state.get('wait') == 'budget':
            return {'wait': 'budget_extended' if reply[0]['reply'] == '继续（增加20轮、60次工具）' else 'budget_stopped'}
        updated = {**result, 'status': 'success', 'data': {**result.get('data', {}), 'reply': reply[0]['reply']}}
        return {'results': [*state['results'][:-1], updated]}

    async def advance(state):
        return {'cursor': state['cursor'] + 1}

    def after_advance(state):
        return 'dispatch' if state['cursor'] < len(state['calls']) else 'collect_results'

    async def collect_results(state):
        runtime.current(state)
        paired = []
        visible = list(state['visible'])
        protected = []
        delivery = state.get('delivery')
        answer = state.get('answer')
        delivery_display_id = state.get('delivery_display_id')
        answer_display_id = state.get('answer_display_id')
        error_counts = dict(state.get('error_counts', {}))
        for call, result in zip(state['calls'], state['results'], strict=True):
            if result['status'] == 'error' and (result.get('error') or {}).get('code') != 'superseded':
                key = call['tool'] + ':' + (result.get('error') or {}).get('code', 'tool_failed')
                error_counts[key] = error_counts.get(key, 0) + 1
            elif result['status'] == 'success':
                error_counts = {key: count for key, count in error_counts.items() if not key.startswith(call['tool'] + ':')}
            paired.append({'id': state['run_id'] + ':' + call['call_id'] + ':result', 'role': 'tool',
                           'call_id': call['call_id'], 'result': result})
            for name in result.get('data', {}).get('activated_tools', []):
                if name in runtime.registry.tools:
                    # Moving the most recently loaded contract to the end
                    # allows the budgeter to reclaim older unrelated tools.
                    visible = [value for value in visible if value != name] + [name]
                    protected.append(name)
            if call['tool'] == 'delivery_publish' and result['status'] == 'success':
                delivery = result
                delivery_display_id = runtime.confirm_response(state, call, result)
            elif call['tool'] == 'conversation_answer' and result['status'] == 'success':
                answer = result
                answer_display_id = runtime.confirm_response(state, call, result)
            elif runtime.registry.tools.get(call['tool']) and runtime.registry.tools[call['tool']].effect != 'read':
                delivery = None
                delivery_display_id = None
        with runtime.store.transaction() as db:
            db.execute('UPDATE runs SET rounds=?,tool_count=? WHERE id=? AND generation=?',
                       (state['step'] + 1, state['tool_count'], state['run_id'], state['generation']))
        return {'messages': paired, 'visible': visible, 'protected_tools': protected,
                'step': state['step'] + 1, 'delivery': delivery,
                'answer': answer, 'answer_display_id': answer_display_id, 'delivery_display_id': delivery_display_id,
                'error_counts': error_counts,
                'wait': 'error_limit' if any(count >= 3 for count in error_counts.values()) else state.get('wait')}

    def after_collect(state):
        return 'finalize' if state.get('answer') or state.get('wait') in {'error_limit', 'budget_stopped'} else 'receive_input'

    async def finalize(state):
        runtime.current(state)
        runtime.activity(state, 'finalizing')
        if state.get('wait') not in {'budget_stopped', 'error_limit'} and runtime.inbox(state)[0]:
            # A goal can arrive while a final controller reply is in flight.
            # Use a new model request identity and require a delivery for that
            # goal, instead of closing with the previous goal's old receipt.
            return {'wait': 'new_input', 'step': state['step'] + 1, 'delivery': None, 'answer': None,
                    'delivery_display_id': None, 'answer_display_id': None}
        delivery = state.get('delivery')
        answer = state.get('answer')
        if answer and state.get('wait') not in {'error_limit', 'budget_stopped'}:
            runtime.status(state, 'answered')
            runtime.event(state, state['run_id'] + ':answer', 'message', {'role': 'assistant',
                'content': answer['data']['conversation_answer'], 'references': answer['result_refs'],
                'response_type': 'discussion', 'display_id': state.get('answer_display_id')})
            runtime.close_presentations(state, 'answered')
            return {'final_text': answer['data']['conversation_answer'], 'wait': None}
        if not delivery or not delivery.get('data', {}).get('verified'):
            status = 'incomplete'
            text = '本轮未形成已核实的交付。请查看工具步骤与实际任务结果。'
        else:
            status = 'partial' if delivery['data']['completeness'] == 'partial' else 'succeeded'
            text = delivery['summary']
        runtime.status(state, status)
        runtime.event(state, state['run_id'] + ':delivery', 'delivery', {'status': status,
                      'content': text, 'delivery': delivery, 'display_id': state.get('delivery_display_id')})
        runtime.close_presentations(state, status, text if status == 'incomplete' else '')
        return {'final_text': text, 'wait': None}

    builder = StateGraph(AgentState)
    for name, handler in [('receive_input', receive_input), ('model', model), ('validate_calls', validate_calls),
                          ('dispatch', dispatch), ('await_jobs', await_jobs), ('await_user', await_user),
                          ('advance', advance), ('collect_results', collect_results), ('finalize', finalize)]:
        builder.add_node(name, handler)
    builder.add_edge(START, 'receive_input')
    builder.add_edge('receive_input', 'model')
    builder.add_conditional_edges('model', after_model)
    builder.add_conditional_edges('validate_calls', after_validation)
    builder.add_conditional_edges('dispatch', after_dispatch)
    builder.add_conditional_edges('await_jobs', lambda state: 'await_user' if state['results'][-1]['status'] in {'needs_user', 'needs_verification'} else 'advance')
    builder.add_conditional_edges('await_user', lambda state: 'finalize' if state.get('wait') == 'budget_stopped' else
                                  'receive_input' if state.get('wait') == 'budget_extended' else 'advance')
    builder.add_conditional_edges('advance', after_advance)
    builder.add_conditional_edges('collect_results', after_collect)
    builder.add_conditional_edges('finalize', lambda state: 'receive_input' if state.get('wait') == 'new_input' else END)
    return builder.compile(checkpointer=saver)
