"""One provider call for an official DSH child; its loop remains in DSH."""
from copy import deepcopy
import time

from .context import request_metrics, wire_json
from .providers import payload_for, request, ProviderFault
from .store import canonical, digest, now


async def model(runtime, parent, params):
    runtime.current(parent)
    session_id = params['agent_session_id']
    with runtime.store.transaction() as db:
        db.execute('INSERT OR IGNORE INTO harness_child_sessions VALUES(?,?,0,?)',
                   (session_id, parent['run_id'], now()))
    child = runtime.store.rows('SELECT * FROM harness_child_sessions WHERE session_id=?', (session_id,))[0]
    if child['run_id'] != parent['run_id']:
        # A continuable child may be activated by its parent on a later turn.
        with runtime.store.transaction() as db:
            db.execute('UPDATE harness_child_sessions SET run_id=? WHERE session_id=?', (parent['run_id'], session_id))
    config = dict(runtime.harness_config(parent))
    schemas = {item['name']: {**item['parameters'], 'description': item['description']} for item in params['tools']}
    payload = payload_for(config, params['messages'], schemas=schemas, system=params['system'])
    payload['stream'] = config.get('streaming', True)
    metrics = request_metrics(config, payload, messages=params['messages'], system=params['system'])
    if metrics['pressure_ratio'] > 1:
        raise ProviderFault('CONTEXT_WINDOW_EXCEEDED', '子助手输入超过已配置模型预算，未发送', context_metrics=metrics)
    ordinal = runtime.store.rows('SELECT COALESCE(MAX(step),-1)+1 AS n FROM model_requests WHERE run_id=?', (parent['run_id'],))[0]['n']
    record, _ = runtime.store.begin_model(parent['run_id'], parent['generation'], ordinal, digest(payload))
    with runtime.store.transaction() as db:
        db.execute('INSERT INTO harness_model_calls VALUES(?,?,?,?)',
                   (record['id'], 'subagent', metrics['payload_sha256'], canonical(config)))
        db.execute('INSERT INTO harness_model_payloads VALUES(?,?,?,?)',
                   (record['id'], wire_json(payload), metrics['payload_sha256'], canonical(metrics)))
    runtime.event(parent, record['id'] + ':request', 'model_request', {
        'model': config['model'], 'purpose': 'subagent', 'agent_session_id': session_id,
        'context_metrics': metrics, 'engineering_provider': bool(runtime.model_provider)})
    async def preview(frame):
        runtime.current(parent)
        runtime.store.public_event(parent['run_id'], parent['generation'],
            record['id'] + ':preview:' + digest(frame), 'subagent_preview', {
                'agent_session_id': session_id, 'request_id': record['id'], **frame})
    started = time.monotonic()
    try:
        if runtime.model_provider:
            local = {**deepcopy(parent), 'agent_session_id': session_id,
                     'step': child['model_calls'], 'messages': params['messages']}
            response = await runtime.model_provider(local, schemas)
        else:
            _, secret = runtime.connection.resolve(config['revision'])
            response = await request(config, secret, payload, record['id'], on_preview=preview)
    except ProviderFault as error:
        if error.code != 'response_unknown':
            with runtime.store.transaction() as db:
                db.execute("UPDATE model_requests SET state='rejected',response=? WHERE id=?",
                           (canonical({'error': error.code}), record['id']))
        raise
    response = {**response, 'request_id': record['id'], 'selected_tools': list(schemas)}
    runtime.store.finish_model(record['id'], response)
    with runtime.store.transaction() as db:
        db.execute('UPDATE harness_child_sessions SET model_calls=model_calls+1,updated=? WHERE session_id=?', (now(), session_id))
    runtime.event(parent, record['id'] + ':usage', 'usage', {**response.get('usage', {}),
        'purpose': 'subagent', 'agent_session_id': session_id, 'elapsed_seconds': time.monotonic() - started})
    return response
