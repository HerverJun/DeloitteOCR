"""Convert quiesced legacy evidence without executing its graph or any tools.

SQLite backup absorbs committed WAL pages. The immutable snapshot is the
migration authority; retries never replace it with newer live data.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from workbench_platform.supervisor.processes import require_f_path
from .store import canonical, digest, now, Conflict
from .registry import error_result

VERSION = 'clear-harness-migration-v2'
LEGACY_TABLES = ('sessions', 'runs', 'events', 'operations', 'model_requests', 'inbox',
                 'decisions', 'public_responses', 'derived_inputs', 'audit_tasks',
                 'harness_runs', 'harness_events', 'harness_sessions', 'harness_migrations')
ACTIVE = {'running', 'waiting_job', 'waiting_user', 'needs_verification', 'paused'}


def readonly(path):
    db = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA temp_store=MEMORY')
    return db


def _integrity(db):
    if [row[0] for row in db.execute('PRAGMA integrity_check')] != ['ok']:
        raise Conflict('Legacy SQLite evidence failed its integrity check')


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _logical_digest(path, *, checkpoint=False):
    if not path.is_file():
        return None
    with closing(readonly(path)) as db:
        tables = _tables(db)
        selected = sorted(tables - {'sqlite_sequence'}) if checkpoint else [t for t in LEGACY_TABLES if t in tables]
        result = {}
        for table in selected:
            rows = [tuple(bytes(value).hex() if isinstance(value, bytes) else value for value in row)
                    for row in db.execute('SELECT * FROM "' + table.replace('"', '""') + '"')]
            result[table] = digest(sorted(rows, key=canonical))
        return digest(result)


def _sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.pending-' + uuid.uuid4().hex)
    with temporary.open('x', encoding='utf-8') as stream:
        stream.write(canonical(value))
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


def _legacy_sessions(db, checkpoint_path):
    """New Harness histories are not legacy merely because they share SQLite."""
    tables = _tables(db)
    checkpoint_runs = set()
    if checkpoint_path.is_file():
        with closing(readonly(checkpoint_path)) as checkpoints:
            if 'checkpoints' in _tables(checkpoints):
                checkpoint_runs = {row[0] for row in checkpoints.execute('SELECT DISTINCT thread_id FROM checkpoints')}
    result = []
    for row in db.execute('SELECT id FROM sessions ORDER BY created,id'):
        session_id = row[0]
        old_runs = {run[0] for run in db.execute('SELECT id FROM runs WHERE session_id=?', (session_id,))}
        already_migrated = 'harness_migrations' in tables and db.execute('SELECT 1 FROM harness_migrations WHERE session_id=?', (session_id,)).fetchone()
        harness_owned = (
            ('harness_events' in tables and db.execute('SELECT 1 FROM harness_events WHERE session_id=? LIMIT 1', (session_id,)).fetchone()) or
            ('harness_runs' in tables and db.execute('SELECT 1 FROM harness_runs h JOIN runs r ON r.id=h.run_id WHERE r.session_id=? LIMIT 1', (session_id,)).fetchone()) or
            ('harness_sessions' in tables and db.execute('SELECT 1 FROM harness_sessions WHERE session_id=? LIMIT 1', (session_id,)).fetchone()))
        if already_migrated or old_runs & checkpoint_runs or not harness_owned:
            result.append(session_id)
    return result


def legacy_snapshot(store):
    """Requires the old writer to be stopped; detect writes during two-DB copy."""
    archive = require_f_path(store.root / 'harness' / 'legacy')
    archive.mkdir(parents=True, exist_ok=True)
    root = archive / 'snapshot-v2'
    manifest_path = root / 'snapshot.json'
    if root.exists():
        if not manifest_path.is_file():
            raise Conflict('Legacy snapshot is incomplete; original evidence was not replaced')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('version') != VERSION:
            raise Conflict('Unknown legacy snapshot version')
        for name, record in manifest['databases'].items():
            path = root / name
            if name not in {'operations.sqlite3', 'checkpoints.sqlite3'} or not path.is_file() or _sha(path) != record['sha256']:
                raise Conflict('Legacy snapshot hash mismatch')
            with closing(readonly(path)) as db:
                _integrity(db)
        return root, manifest
    sources = [store.path, store.root / 'checkpoints.sqlite3']
    before = {path.name: _logical_digest(path, checkpoint=path.name == 'checkpoints.sqlite3') for path in sources}
    pending = archive / ('snapshot-pending-' + uuid.uuid4().hex)
    pending.mkdir()
    databases = {}
    for source in sources:
        if not source.is_file():
            continue
        target = pending / source.name
        with closing(readonly(source)) as original, closing(sqlite3.connect(target)) as backup:
            original.backup(backup)
            # Standalone archives must not rely on mutable WAL/SHM siblings.
            backup.execute('PRAGMA journal_mode=DELETE')
            _integrity(backup)
        databases[source.name] = {'sha256': _sha(target), 'source_logical_sha256': before[source.name],
            'committed_wal_included': True, 'source_wal_present': source.with_name(source.name + '-wal').is_file()}
    after = {path.name: _logical_digest(path, checkpoint=path.name == 'checkpoints.sqlite3') for path in sources}
    copied = {path.name: _logical_digest(pending / path.name, checkpoint=path.name == 'checkpoints.sqlite3') for path in sources}
    if before != after or before != copied:
        _atomic_json(pending / 'rejected.json', {'before': before, 'after': after, 'copied': copied})
        raise Conflict('Legacy databases changed during snapshot; stop the legacy writer before migration')
    with closing(readonly(pending / 'operations.sqlite3')) as db:
        sessions = _legacy_sessions(db, pending / 'checkpoints.sqlite3')
    manifest = {'version': VERSION, 'created': now(), 'databases': databases, 'session_ids': sessions,
                'source_root': str(store.root), 'legacy_writer_quiescence_required': True}
    _atomic_json(pending / 'snapshot.json', manifest)
    pending.rename(root)
    return root, manifest


def checkpoint_states(path):
    if not path.is_file():
        return {}
    from .dependencies import load_support_dependencies
    load_support_dependencies()
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    serializer = JsonPlusSerializer(pickle_fallback=False)
    result = {}
    with closing(readonly(path)) as db:
        _integrity(db)
        if 'checkpoints' not in _tables(db):
            return result
        columns = {row[1] for row in db.execute('PRAGMA table_info(checkpoints)')}
        namespace = " WHERE checkpoint_ns=''" if 'checkpoint_ns' in columns else ''
        query = 'SELECT thread_id,checkpoint_id,type,checkpoint FROM checkpoints' + namespace + ' ORDER BY thread_id,checkpoint_id'
        for identifier, _, kind, payload in db.execute(query):
            try:
                value = serializer.loads_typed((kind, payload))
                channels = value['channel_values']
                if not isinstance(channels, dict) or not isinstance(channels.get('messages', []), list):
                    raise ValueError('Invalid checkpoint channels')
            except Exception as error:
                raise Conflict('Legacy checkpoint cannot be decoded safely: ' + str(identifier)) from error
            result[identifier] = channels
    return result


def paired_history(messages, operations, run_id, checkpoint_results=None):
    """Pair only recorded calls; never submit or claim an unobserved success."""
    by_call = {row['call_id']: row for row in operations}
    checkpoint_results = checkpoint_results or {}
    result, outstanding, seen = [], [], set()

    def receipt(call):
        record = by_call.get(call['call_id'])
        if record and record.get('result'):
            return json.loads(record['result'])
        if call['call_id'] in checkpoint_results:
            return checkpoint_results[call['call_id']]
        return error_result('response_unknown' if record else 'not_started',
            '原操作回执待核实，恢复时查询原生状态' if record else '迁移前尚未提交此操作',
            status='needs_verification' if record else 'error')

    def close_pairs():
        for call in outstanding:
            result.append({'id': run_id + ':' + call['call_id'] + ':migration-pair', 'role': 'tool',
                'call_id': call['call_id'], 'result': receipt(call),
                'migration_source': {'kind': 'operation_receipt' if call['call_id'] in by_call else 'unsubmitted_call',
                                     'run_id': run_id, 'replayed': False}})
        outstanding.clear()

    for index, original in enumerate(messages):
        message = deepcopy(original)
        if message.get('role') not in {'user', 'assistant', 'tool'}:
            raise Conflict('Unsupported legacy message role')
        message.setdefault('id', run_id + ':legacy:' + str(index) + ':' + digest(message)[:12])
        if message['role'] != 'tool' and outstanding:
            close_pairs()
        if message['role'] == 'assistant':
            for call in message.get('calls', []):
                if call['call_id'] in seen:
                    raise Conflict('Duplicate legacy tool-call identity')
                seen.add(call['call_id'])
                record = by_call.get(call['call_id'])
                if record and (record['tool'] != call['tool'] or json.loads(record['arguments']) != call['arguments']):
                    raise Conflict('Legacy operation parameters differ from the model call')
                outstanding.append(call)
        elif message['role'] == 'tool':
            matching = next((call for call in outstanding if call['call_id'] == message['call_id']), None)
            if matching is None:
                raise Conflict('Legacy tool/result identity is inconsistent')
            outstanding.remove(matching)
            record = by_call.get(message['call_id'])
            if record and record.get('result'):
                latest = json.loads(record['result'])
                original_result = message['result']
                if original_result != latest:
                    if original_result.get('status') not in {'pending', 'needs_user', 'needs_verification'}:
                        raise Conflict('Conflicting terminal legacy tool receipts')
                    message['result'] = latest
                    message['migration_source'] = {'kind': 'settled_operation_receipt', 'run_id': run_id,
                        'original_result_sha256': digest(original_result), 'original_result': original_result,
                        'replayed': False}
        result.append(message)
    close_pairs()
    return result


def _run_history(db, run, state):
    run_id = run['id']
    operations = [dict(row) for row in db.execute('SELECT * FROM operations WHERE run_id=? ORDER BY created,id', (run_id,))]
    models = [dict(row) for row in db.execute('SELECT * FROM model_requests WHERE run_id=? ORDER BY step,created,id', (run_id,))]
    events = [dict(row) for row in db.execute('SELECT * FROM events WHERE run_id=? ORDER BY seq', (run_id,))]
    messages = deepcopy(state.get('messages', []))
    known = {message.get('id'): message for message in messages if message.get('id')}
    missing = []
    for row in models:
        if row['state'] != 'received':
            continue
        if not row['response']:
            raise Conflict('Received legacy model request has no response')
        response = json.loads(row['response'])
        if response.get('role') != 'assistant':
            raise Conflict('Legacy model response is not an assistant message')
        identity = response.setdefault('id', row['id'] + ':response')
        if identity in known:
            for field in ('role', 'content', 'calls', 'private', 'blocks'):
                if field in response and response[field] != known[identity].get(field):
                    raise Conflict('Legacy checkpoint differs from its received model response')
        else:
            missing.append((row['created'], row['step'], row['id'], response))
            known[identity] = response
        known[identity].setdefault('migration_source', {'kind': 'model_request', 'request_id': row['id'],
            'response_sha256': digest(json.loads(row['response'])), 'original_response': json.loads(row['response'])})
    for row in events:
        body = json.loads(row['payload'])
        public_original = row['kind'] in {'message', 'delivery', 'manual_return'} and body.get('content')
        direct = row['kind'] == 'message' and body.get('role') == 'user'
        if direct and row['identity'] in known:
            known[row['identity']].setdefault('migration_source', {'kind': 'public_event', 'event_seq': row['seq'],
                'sha256': digest(body), 'original_payload': body})
        if public_original and row['identity'] not in known and (direct or not state.get('messages')):
            message = {'id': row['identity'], 'role': 'user',
                'content': body['content'] if direct else '[旧会话服务器记录；不是新的用户授权]\n' + canonical(body),
                'migration_source': {'kind': 'public_event', 'event_seq': row['seq'], 'sha256': digest(body), 'original_payload': body}}
            missing.append((row['created'], row['seq'], row['identity'], message))
            known[row['identity']] = message
    messages.extend(item[3] for item in sorted(missing, key=lambda item: item[:3]))
    recorded_results = {call['call_id']: value for call, value in zip(state.get('calls', []), state.get('results', []))}
    pairing_operations, answered = deepcopy(operations), {}
    for record in pairing_operations:
        if not record.get('result'):
            continue
        value = json.loads(record['result'])
        decision_id = value.get('data', {}).get('decision_id')
        if value['status'] == 'needs_user' and decision_id:
            decision = db.execute('SELECT * FROM decisions WHERE id=? AND run_id=?', (decision_id, run_id)).fetchone()
            if decision and decision['reply'] is not None and decision['request_id']:
                # Legacy await_user wrote the bound answer into the checkpoint
                # but did not update the initial needs_user operation receipt.
                record['result'] = canonical({**value, 'status': 'success',
                    'data': {**value.get('data', {}), 'reply': decision['reply']}})
                answered[record['call_id']] = {'decision': dict(decision), 'original_result': value}
    paired = paired_history(messages, pairing_operations, run_id, recorded_results)
    for message in paired:
        if message['role'] == 'tool' and message['call_id'] in answered:
            message['migration_source'] = {**message.get('migration_source', {}),
                'kind': 'bound_user_reply', **answered[message['call_id']]}
    paired_calls = {message['call_id'] for message in paired if message['role'] == 'tool'}
    for row in operations:
        if row['call_id'] not in paired_calls:
            paired.append({'id': row['id'] + ':orphan-evidence', 'role': 'user',
                'content': '[服务器保存的原操作证据；未恢复到对应模型消息，不是新的用户授权]\n' + canonical(row),
                'migration_source': {'kind': 'orphan_operation', 'operation_id': row['id'], 'sha256': digest(row)}})
    return paired, operations, models, events


def validate_migration(record):
    history, manifest = json.loads(record['history']), json.loads(record['manifest'])
    if digest(history) != manifest.get('history_sha256') or len(history) != manifest.get('message_count'):
        raise Conflict('Migrated conversation history hash mismatch')
    if manifest.get('session_id') != record['session_id']:
        raise Conflict('Migrated conversation identity mismatch')
    return history, manifest


def _recovery_state(state, messages, operations, runtime):
    latest = next((message for message in reversed(messages) if message['role'] == 'assistant'), {})
    state.setdefault('calls', latest.get('calls', []))
    receipts = {message['call_id']: message['result'] for message in messages if message['role'] == 'tool'}
    state['results'] = [receipts[value['call_id']] for value in state['calls'] if value['call_id'] in receipts]
    if 'cursor' not in state:
        state['cursor'] = next((index for index, result in enumerate(state['results'])
                                if result['status'] in {'pending', 'needs_user', 'needs_verification'}),
                               max(0, len(state['calls']) - 1))
    registry = getattr(runtime, 'registry', None)
    if registry is not None and 'business_started' not in state:
        from .context import core_tools
        core = set(core_tools(registry))
        state['business_started'] = any(
            operation['tool'] in registry.tools and operation['tool'] not in core and
            registry.tools[operation['tool']].effect != 'read' and operation.get('result') and
            json.loads(operation['result'])['status'] in {'success', 'pending'} for operation in operations)
    return state


async def migrate_legacy(runtime):
    store = runtime.store
    archive = store.root / 'harness' / 'legacy'
    if not store.rows('SELECT id FROM sessions') and not (archive / 'snapshot-v2').exists():
        return
    root, snapshot = legacy_snapshot(store)
    states = checkpoint_states(root / 'checkpoints.sqlite3')
    reports = []
    with closing(readonly(root / 'operations.sqlite3')) as source:
        for session_id in snapshot['session_ids']:
            existing = store.rows('SELECT * FROM harness_migrations WHERE session_id=?', (session_id,))
            if existing:
                _, manifest = validate_migration(existing[0])
                if manifest.get('version') == VERSION and manifest.get('snapshot_sha256') != digest(snapshot):
                    raise Conflict('Migrated conversation references a different legacy snapshot')
                if manifest.get('version') == VERSION:
                    reports.append(manifest)
                    continue
            session = dict(source.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone())
            history, identities, run_reports = [], {}, []
            runs = [dict(row) for row in source.execute('SELECT * FROM runs WHERE session_id=? ORDER BY created,id', (session_id,))]
            active_states = []
            scoped = []
            def append_message(message):
                old = identities.get(message['id'])
                if old is not None and old != message:
                    raise Conflict('Legacy message identity has conflicting content')
                if old is None:
                    history.append(message)
                    identities[message['id']] = message
            for row in source.execute('SELECT * FROM events WHERE session_id=? AND run_id IS NULL ORDER BY created,seq', (session_id,)):
                body = json.loads(row['payload'])
                if body.get('content'):
                    message = {'id': row['identity'], 'role': 'user', 'content': body['content'] if body.get('role') == 'user'
                               else '[旧会话服务器记录；不是新的用户授权]\n' + canonical(body),
                        'migration_source': {'kind': 'public_event', 'event_seq': row['seq'],
                            'sha256': digest(body), 'original_payload': body}}
                    scoped.append((row['created'], message))
            for run in runs:
                while scoped and scoped[0][0] <= run['created']:
                    append_message(scoped.pop(0)[1])
                state = deepcopy(states.get(run['id'], {}))
                messages, operations, models, events = _run_history(source, run, state)
                for message in messages:
                    append_message(message)
                run_reports.append({'run_id': run['id'], 'status': run['status'], 'generation': run['generation'],
                    'checkpoint_present': bool(state), 'messages_sha256': digest(messages),
                    'model_count': len(models), 'models_sha256': digest(models),
                    'operation_count': len(operations), 'operations_sha256': digest(operations),
                    'event_count': len(events), 'events_sha256': digest(events),
                    'unanswered_model_requests': [model['id'] for model in models if model['state'] == 'sent']})
                if run['status'] in ACTIVE:
                    state.update(run_id=run['id'], session_id=session_id, workspace_id=session['workspace_id'],
                        generation=run['generation'], request_id=run['request_id'], messages=messages,
                        continuation_authorized=True, migrated_from='langgraph', entered=True)
                    state.setdefault('visible', [])
                    state.setdefault('step', run['rounds'])
                    state.setdefault('tool_count', run['tool_count'])
                    active_states.append(_recovery_state(state, messages, operations, runtime))
            for _, message in scoped:
                append_message(message)
            manifest = {'version': VERSION, 'session_id': session_id, 'session_sha256': digest(session), 'run_count': len(runs),
                'message_count': len(history), 'history_sha256': digest(history), 'runs': run_reports,
                'run_statuses': {run['id']: run['status'] for run in runs},
                'original_checkpoint_retained': 'checkpoints.sqlite3' in snapshot['databases'],
                'operation_count': sum(run['operation_count'] for run in run_reports),
                'snapshot_sha256': digest(snapshot), 'materialized': False}
            with store.transaction() as db:
                for run in runs:
                    current = db.execute('SELECT generation,status FROM runs WHERE id=?', (run['id'],)).fetchone()
                    if not current or current['generation'] != run['generation'] or current['status'] != run['status']:
                        raise Conflict('Legacy run changed after the frozen snapshot')
                for state in active_states:
                    db.execute('INSERT OR REPLACE INTO harness_runs VALUES(?,?,?)', (state['run_id'], canonical(state), now()))
                db.execute('INSERT OR REPLACE INTO harness_migrations VALUES(?,?,?,?)', (session_id, canonical(history), canonical(manifest), now()))
                for run in runs:
                    if run.get('round_limit') == 20 and run.get('tool_limit') == 60:
                        db.execute('UPDATE runs SET round_limit=0,tool_limit=0 WHERE id=? AND round_limit=20 AND tool_limit=60', (run['id'],))
            reports.append(manifest)
            _atomic_json(archive / 'migration-v2.json', {'version': VERSION, 'snapshot_sha256': digest(snapshot),
                'session_count': len(snapshot['session_ids']), 'converted_count': len(reports), 'sessions': reports})
    _atomic_json(archive / 'migration-v2.json', {'version': VERSION, 'snapshot_sha256': digest(snapshot),
        'session_count': len(snapshot['session_ids']), 'converted_count': len(reports), 'sessions': reports})
