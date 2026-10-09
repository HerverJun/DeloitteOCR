"""One dynamic LangGraph. Business tool choices belong to the controller."""
import json

from .dependencies import load_dependencies
from .registry import invoke, error_result, tool_activity
from dataclasses import replace
from .state import AgentState


def screen_pending(result):
    refs = result.get('job_refs', [])
    return bool(refs) and all(ref.get('application') == 'ocr' and
        ref.get('coverage', {}).get('kind') == 'screen_batch' for ref in refs)


def screen_waiting(result):
    refs = result.get('job_refs', [])
    return bool(refs) and all(ref.get('application') == 'ocr' and
        ref.get('coverage', {}).get('kind') in {'screen_batch', 'screen_local_candidates'} for ref in refs)


def screen_controls(results):
    kinds = {ref.get('coverage', {}).get('kind') for result in results for ref in result.get('job_refs', [])}
    return [name for kind, name in (('screen_batch', 'ocr_screen_batch_control'),
        ('screen_local_candidates', 'ocr_screen_local_candidates_control')) if kind in kinds]


def terminal_screen_delivery(state,delivery):
    """Only a fully accounted export scope over settled jobs can end directly."""
    data=delivery.get('data',{})
    if state.get('background_jobs') or not data.get('verified') or data.get('continuing_job_refs'):
        return False
    jobs={}
    for message in state.get('messages',[]):
        if message['role']!='tool':continue
        receipt=message['result'];refs=receipt.get('job_refs',[])
        if receipt['status'] in {'pending','needs_verification'}:return False
        if not refs:continue
        if not screen_pending(receipt):return False
        coverage=receipt.get('coverage',{})
        if receipt['status']!='success' or coverage.get('pending')!=0 or type(coverage.get('total')) is not int:
            return False
        for ref in refs:jobs[ref['object_id']]=coverage
    if not jobs:return False
    exports={item['batch_id']:item for item in data.get('screen_batch_coverage',[])}
    return all(batch_id in exports and exports[batch_id].get('pending')==0 and
        exports[batch_id].get('current_verified') is True and
        exports[batch_id].get('total')==coverage['total'] and
        exports[batch_id].get('file_count', 0)+exports[batch_id].get('settled_without_file', 0)==
        coverage.get('completed',0)+coverage.get('needs_review',0) for batch_id,coverage in jobs.items())


def terminal_workflow_delivery(state, delivery, route):
    """End a completed fixed workflow only with its latest exact export/evidence."""
    data = delivery.get('data', {})
    if (not route or route.get('phase') != 'verified_delivery' or state.get('background_jobs')
            or data.get('verified') is not True or data.get('completeness') != 'complete'
            or data.get('continuing_job_refs') or delivery.get('status') != 'success'):
        return False
    workflow = route.get('workflow')
    tools = {'existing_ocr_reconciliation': ('data_export', 'data_read_result', 'data_session_id'),
             'classification': ('classification_export', 'classification_read_results', 'handoff_id')}
    if workflow not in tools:
        return False
    if not any(call['tool'] == 'delivery_publish' and result == delivery
               for call, result in zip(state.get('calls', ()), state.get('results', ()), strict=True)):
        return False
    calls = {call['call_id']: call for message in state.get('messages', ())
             for call in message.get('calls', ()) if message.get('role') == 'assistant'}
    paired = [(calls[message['call_id']], message['result']) for message in state.get('messages', ())
              if message.get('role') == 'tool' and message.get('call_id') in calls]
    if any(result.get('status') in {'pending', 'needs_user', 'needs_verification'} for _, result in paired):
        return False
    export_tool, read_tool, scope_key = tools[workflow]
    def latest(tool):
        return next(((call, result) for call, result in reversed(paired) if call['tool'] == tool), None)
    exported, read, evidence = latest(export_tool), latest(read_tool), latest('audit_read_evidence')
    if not all(entry and entry[1].get('status') == 'success' for entry in (exported, read, evidence)):
        return False
    if (not exported[0]['arguments'].get(scope_key)
            or exported[0]['arguments'][scope_key] != read[0]['arguments'].get(scope_key)
            or read[1].get('next_offset') is not None or evidence[1].get('next_offset') is not None
            or evidence[1].get('data', {}).get('stale') is not False):
        return False
    fields = ('kind', 'workspace_id', 'object_id', 'revision', 'sha256', 'application', 'parent_id')
    def identity(ref):
        return tuple(ref.get(key) for key in fields)
    expected_artifacts = {identity(ref) for ref in exported[1].get('artifact_refs', ())}
    expected_results = {identity(ref) for entry in (read, evidence) for ref in entry[1].get('result_refs', ())}
    if (not expected_artifacts or not read[1].get('result_refs') or not evidence[1].get('result_refs')
            or not expected_artifacts <= {identity(ref) for ref in delivery.get('artifact_refs', ())}
            or not expected_results <= {identity(ref) for ref in delivery.get('result_refs', ())}):
        return False
    published = {item.get('bundle_id') for item in data.get('delivery', ()) if item.get('files')}
    return all(ref[2] in published for ref in expected_artifacts)


def build_graph(runtime, saver):
    StateGraph, _ = load_dependencies()
    from langgraph.graph import START, END
    from langgraph.types import interrupt

    async def receive_input(state):
        runtime.current(state)
        changed_scope = runtime.tasks.apply_scope(state['session_id'])
        additions, identifiers = runtime.inbox(state)
        if changed_scope:
            from .scope import scope_summary
            bounded = {key: value for key, value in changed_scope.items()
                       if key not in {'selected_assets', 'pending_scope'}}
            bounded.update(scope_summary(runtime.context(state)))
            additions.append({'id': state['run_id'] + ':scope:' + str(changed_scope['scope_revision']),
                              'role': 'user', 'content': '[服务器已应用用户明确变更的资料范围]\n' + json.dumps(bounded, ensure_ascii=False)})
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
        call = state['calls'][state['cursor']]
        async def detach_if_directed():
            if not screen_waiting(result) or not runtime.inbox(state)[0]:
                return None
            record = runtime.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?',
                                        (state['run_id'], call['call_id']))[0]
            background = [item for item in state.get('background_jobs', []) if item['operation_id'] != record['id']]
            background.append({'operation_id': record['id'], 'call': call, 'result': result})
            # This is a confirmed submission receipt, not a completed OCR
            # receipt. Keep the original operation pending in the ledger.
            submitted = {**result, 'status': 'success',
                'summary': '整批图片仍在后台处理；已收到追加指令，先处理该指令',
                'data': {**result.get('data', {}), 'background_pending': True}}
            runtime.status(state, 'running')
            # The durable pending batch makes its control contract relevant;
            # expose it directly instead of making a midrun directive discover
            # the catalog again. The controller still chooses the action.
            controls=screen_controls([item['result'] for item in background])
            visible=list(state['visible'])
            for control in controls:
                if control in runtime.registry.tools and control not in visible:
                    visible.append(control)
            return {'results': [*state['results'][:-1], submitted], 'background_jobs': background,
                    'visible':visible,'protected_tools':[control for control in controls if control in visible]}
        settled = await runtime.settle(result, state)
        if settled is None:
            detached = await detach_if_directed()
            if detached:
                return detached
            runtime.status(state, 'waiting_job')
            runtime.activity(state, 'waiting_job', job_refs=result['job_refs'])
            interrupt({'kind': 'native_jobs', 'job_refs': result['job_refs']})
            # The node rechecks authoritative job state when resumed, never
            # accepts a browser-supplied tool result as a native receipt.
            settled = await runtime.settle(result, state)
            if settled is None:
                detached = await detach_if_directed()
                if detached:
                    return detached
                raise ValueError('Native job observer resumed before settlement')
        runtime.current(state)
        finish_job(state, call, settled)
        runtime.status(state, 'running')
        return {'results': [*state['results'][:-1], settled]}

    def finish_job(state, call, settled, operation_id=None):
        record = runtime.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?',
                                    (state['run_id'], call['call_id']))[0]
        if operation_id and operation_id != record['id']:
            raise ValueError('Background operation identity changed')
        runtime.store.finish_operation(record['id'], settled)
        tool_activity(replace(runtime.context(state), operation_id=record['id']), call, 'finished', settled)
        runtime.event(state, call['call_id'] + ':finished', 'tool_step', {'tool': call['tool'], 'call_id': call['call_id'],
            'status': settled['status'], 'summary': settled['summary'], 'artifact_refs': settled['artifact_refs'],
            'coverage': settled['coverage']})

    async def await_background(state):
        # A screen-specific checkpoint owns the original native job while the
        # controller answers pause/export/upgrade directives. Native progress
        # alone never starts a controller round before final settlement.
        while True:
            runtime.current(state)
            pending, paired, finished = [], [], []
            for item in state.get('background_jobs', []):
                settled = await runtime.settle(item['result'], state)
                if settled is None:
                    pending.append(item)
                    continue
                finish_job(state, item['call'], settled, item['operation_id'])
                paired.append({'id': state['run_id'] + ':' + item['call']['call_id'] + ':result',
                               'role': 'tool', 'call_id': item['call']['call_id'], 'result': settled})
                finished.append({'operation_id':item['operation_id'],'job_refs':[
                    {key:ref[key] for key in ('kind','application','object_id')} for ref in item['result']['job_refs']],
                    'coverage':settled.get('coverage',{})})
            if finished and pending and not runtime.inbox(state)[0]:
                # Persist each settled operation before waiting on the other
                # original/upgrade jobs. A checkpoint update alone must not
                # call the controller or re-observe already finished jobs.
                return {'messages':paired,'background_jobs':pending,'wait':'background_settle'}
            if not pending or runtime.inbox(state)[0]:
                if finished and not pending:
                    # A user may already have received the resume response.
                    # Replacing an earlier pending tool receipt alone does not
                    # make this later native event visible chronologically.
                    # Signal actual settlement as a new server event, while
                    # retaining the original operation and typed references.
                    paired.append({'id':state['run_id']+':screen-completed:'+':'.join(item['operation_id'] for item in finished),
                        'role':'user','content':'[服务器后台屏拍批次已结束；真实处理覆盖见下方回执]\n'+json.dumps({
                            'completed_jobs':finished,'remaining_background_jobs':0,
                            'continuation':'继续原目标，导出最终批次范围并核实交付；之前部分ZIP不是最终交付，人工复核状态不变。'},ensure_ascii=False)})
                runtime.status(state, 'running')
                return {'messages': paired, 'background_jobs': pending, 'wait': None,
                        'protected_tools':state.get('protected_tools',[]) if pending else [
                            name for name in state.get('protected_tools',[]) if name not in
                            {'ocr_screen_batch_control','ocr_screen_local_candidates_control'}],
                        'answer': None, 'delivery': None, 'answer_display_id': None, 'delivery_display_id': None}
            runtime.status(state, 'waiting_job')
            refs = [ref for item in pending for ref in item['result']['job_refs']]
            runtime.activity(state, 'waiting_job', job_refs=refs)
            interrupt({'kind': 'screen_background', 'job_refs': refs})

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
        protected = [name for name in screen_controls([item['result'] for item in state.get('background_jobs', [])])
                     if name in visible]
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
        delivery=state.get('delivery') or {}
        continuing=delivery.get('data',{}).get('continuing_job_refs',[])
        background={ref['object_id'] for item in state.get('background_jobs',[]) for ref in item['result'].get('job_refs',[])}
        partial_directive=bool(background and continuing and delivery.get('data',{}).get('verified') and
            all(ref.get('object_id') in background for ref in continuing) and any(
                call['tool']=='delivery_publish' and result['status']=='success'
                for call,result in zip(state['calls'],state['results'],strict=True)))
        final_screen=bool(any(call['tool']=='delivery_publish' and result['status']=='success'
            for call,result in zip(state['calls'],state['results'],strict=True)) and terminal_screen_delivery(state,delivery))
        final_workflow = runtime.terminal_workflow_delivery(state, delivery)
        # Only the explicitly verified partial response takes this route. The
        # existing finalize node retains the original job and returns to its
        # background checkpoint; later directives and final delivery continue.
        return 'finalize' if state.get('answer') or partial_directive or final_screen or final_workflow or state.get('wait') in {'error_limit', 'budget_stopped'} else 'receive_input'

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
        if state.get('background_jobs') and state.get('wait') not in {'budget_stopped', 'error_limit'}:
            # Publish the confirmed directive response without ending or
            # claiming success for the still-running original batch.
            if answer:
                runtime.event(state, state['run_id'] + ':directive-answer:' + str(state['step']), 'message',
                    {'role': 'assistant', 'content': answer['data']['conversation_answer'],
                     'references': answer['result_refs'], 'response_type': 'discussion',
                     'display_id': state.get('answer_display_id')})
            elif delivery and delivery.get('data', {}).get('verified'):
                runtime.event(state, state['run_id'] + ':directive-delivery:' + str(state['step']), 'delivery',
                    {'status': 'partial', 'content': delivery['summary'], 'delivery': delivery,
                     'display_id': state.get('delivery_display_id')})
            # A no-call provider response already used this model request
            # identity. Resume completion with a fresh identity after waiting.
            return {'wait': 'background', **({'step': state['step'] + 1} if not state.get('calls') else {})}
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
                          ('await_background', await_background), ('advance', advance), ('collect_results', collect_results), ('finalize', finalize)]:
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
    builder.add_conditional_edges('finalize', lambda state: 'receive_input' if state.get('wait') == 'new_input' else
                                  'await_background' if state.get('wait') == 'background' else END)
    builder.add_conditional_edges('await_background', lambda state: 'await_background'
        if state.get('wait')=='background_settle' else 'receive_input')
    return builder.compile(checkpointer=saver)
