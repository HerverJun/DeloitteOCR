"""Import all converted sessions into official Harness logs without execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

from workbench_platform.supervisor.processes import require_f_path
from .harness import HarnessProcess, installation
from .harness_migration import _atomic_json, validate_migration
from .store import Conflict, canonical, digest, now

VERSION = 'clear-harness-materialization-v1'


class ImportProcess(HarnessProcess):
    """Use the production RPC transport, but admit no model/tool callbacks."""
    def __init__(self, runtime, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}', session_id):
            raise Conflict('Invalid migrated conversation identity')
        self.callbacks = []
        async def forbidden(_state, method, _params):
            self.callbacks.append(method)
            raise Conflict('Import-only Harness requested execution: ' + method)
        engine = SimpleNamespace(runtime=runtime, callback=forbidden, save=lambda _state: None)
        super().__init__(engine, {'run_id': 'import-' + session_id, 'session_id': session_id})

    async def start(self, params):
        source, node = installation()
        root = require_f_path(self.runtime.store.root / 'harness')
        for part in ('sessions', 'temp', 'cache', 'cache/appdata', 'cache/localappdata', 'logs'):
            (root / part).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(TEMP=str(root / 'temp'), TMP=str(root / 'temp'), TMPDIR=str(root / 'temp'),
            APPDATA=str(root / 'cache/appdata'), LOCALAPPDATA=str(root / 'cache/localappdata'),
            XDG_CACHE_HOME=str(root / 'cache'), DSH_HOME=str(root), DSH_RESOURCE_CACHE=str(root / 'cache'), DSH_PRIMARY_RUNTIME='')
        self.log = (root / 'logs' / (self.state['run_id'] + '.log')).open('ab')
        self.process = await asyncio.create_subprocess_exec(str(node), str(source / 'host.mjs'), cwd=str(source), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log,
            limit=16 * 1024 * 1024, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.reader = asyncio.create_task(self.read(), name='harness-import-' + self.state['session_id'])
        result = await self.call('initialize', {**params, 'version': 1, 'import_only': True,
            'root': str(root / 'sessions'), 'cwd': str(self.runtime.store.root), 'session_id': self.state['session_id']}, timeout=45)
        if self.callbacks or result.get('engine') != 'deepseek-harness' or result.get('session_id') != self.state['session_id']:
            raise Conflict('Harness import initialization could not be verified')
        return result


def _text(content):
    return '\n'.join(block['text'] for block in content if block.get('type') == 'text')


def validate_import(history, events):
    """Check exact imported public messages and retained producer provenance."""
    extracted = []
    for event in events:
        kind, data = event['type'], event['data']
        if kind == 'user/message':
            extracted.append({'id': data['id'], 'role': 'user', 'content': _text(data['content']),
                'provenance': data.get('source', {}).get('migration_source')})
        elif kind == 'assistant/message':
            message = data['message']
            replay = message.get('source', {}).get('replayState', {}).get('response', {})
            extracted.append({'id': message['id'], 'role': 'assistant', 'content': _text(message['content']),
                'calls': [{'call_id': block['id'], 'tool': block['name'], 'arguments': json.loads(block['arguments'])}
                          for block in message['content'] if block.get('type') == 'tool-call'],
                'private': replay.get('private', {}), 'blocks': replay.get('blocks'),
                'provenance': replay.get('migration_source')})
        elif kind == 'tool/result':
            message = data['message']
            extracted.append({'id': message['id'], 'role': 'tool', 'call_id': message['toolCallId'],
                'result': json.loads(_text(message['content'])), 'provenance': data.get('meta', {}).get('migration_source')})
    if len(extracted) != len(history):
        raise Conflict('Harness imported message count differs from converted history')
    for expected, actual in zip(history, extracted, strict=True):
        fields = ('id', 'role', 'content') if expected['role'] == 'user' else (
            ('id', 'role', 'content', 'calls') if expected['role'] == 'assistant' else ('id', 'role', 'call_id', 'result'))
        if any(actual[field] != expected.get(field, '' if field == 'content' else []) for field in fields):
            raise Conflict('Harness imported message differs from converted history: ' + str(expected.get('id')))
        if actual['provenance'] != expected.get('migration_source'):
            raise Conflict('Harness import lost original provenance: ' + str(expected.get('id')))
        if expected['role'] == 'assistant' and (actual['private'] != expected.get('private', {}) or actual['blocks'] != expected.get('blocks')):
            raise Conflict('Harness import lost the original model replay blocks')


def _log_path(store, session_id):
    root = require_f_path(store.root / 'harness/sessions')
    matches = []
    for candidate in root.rglob('session*.jsonl'):
        match = re.fullmatch(r'session(?:\.v([0-9]+))?\.jsonl', candidate.name)
        if not match:
            continue
        path = require_f_path(candidate)
        if not path.is_relative_to(root):
            raise Conflict('Harness log escapes its owned session root')
        with path.open('rb') as stream:
            header = json.loads(stream.readline())
        if header.get('type') == 'session' and header.get('id') == session_id:
            matches.append((int(match[1] or 0), path))
    if not matches:
        raise Conflict('Imported Harness session has no durable JSONL log')
    latest = max(version for version, _ in matches)
    chosen = [path for version, path in matches if version == latest]
    if len(chosen) != 1:
        raise Conflict('Harness session has ambiguous durable JSONL logs')
    return chosen[0]


def _physical_record(store, session_id, events):
    path = _log_path(store, session_id)
    stamp, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        header_line = stream.readline()
        header = json.loads(header_line)
        stamp.update(header_line)
        size += len(header_line)
        for expected in events:
            raw = stream.readline()
            if not raw.endswith(b'\n'):
                raise Conflict('Harness imported log has an incomplete committed record')
            actual = json.loads(raw)
            if digest(actual) != digest(expected):
                raise Conflict('Harness JSONL differs from its imported event stream')
            stamp.update(raw)
            size += len(raw)
        if stream.read(1):
            raise Conflict('Harness import log contains unverified additional events')
    return {'path': str(path.relative_to(store.root)), 'prefix_bytes': size,
            'prefix_sha256': stamp.hexdigest(), 'format_version': header['version']}


def _run_mapping(store, session_id, history):
    identities, calls = {}, {}
    runs = store.rows('SELECT id,request_id FROM runs WHERE session_id=?', (session_id,))
    for run in runs:
        identities[run['request_id']] = run['id']
        for record in store.rows('SELECT response FROM model_requests WHERE run_id=? AND response IS NOT NULL', (run['id'],)):
            response = json.loads(record['response'])
            if isinstance(response, dict) and response.get('id'):
                identities[response['id']] = run['id']
    for event in store.rows('SELECT identity,run_id FROM events WHERE session_id=?', (session_id,)):
        identities[event['identity']] = event['run_id'] or ''
    for message in history:
        identifier = message['id']
        run_id = identities.get(identifier, next((run['id'] for run in runs if identifier.startswith(run['id'] + ':')), ''))
        if message['role'] == 'assistant':
            for call in message.get('calls', []):
                calls[call['call_id']] = run_id
        elif message['role'] == 'tool' and not run_id:
            run_id = calls.get(message['call_id'], '')
        identities[identifier] = run_id
    return identities


def _event_run(event, mapping):
    data = event['data']
    message = data.get('message', data)
    return mapping.get(message.get('id'), '')


def _verify_registered(store, row, source):
    manifest = json.loads(row['manifest'])
    if row['manifest_sha256'] != digest(manifest) or row['history_sha256'] != source['history_sha256']:
        raise Conflict('Harness materialization registration hash mismatch')
    hashes = manifest['event_hashes']
    if len(hashes) != row['event_count'] or digest(hashes) != row['event_sha256']:
        raise Conflict('Harness materialized event inventory hash mismatch')
    events = store.rows('SELECT seq,payload,sha256 FROM harness_events WHERE session_id=? AND seq<? ORDER BY seq',
                       (row['session_id'], row['event_count']))
    if len(events) != len(hashes):
        raise Conflict('Harness materialized event projection is incomplete')
    for seq, event in enumerate(events):
        if event['seq'] != seq or event['sha256'] != hashes[seq] or digest(json.loads(event['payload'])) != hashes[seq]:
            raise Conflict('Harness materialized event projection was changed')
    path = require_f_path(store.root / manifest['jsonl']['path'])
    if not path.is_relative_to(require_f_path(store.root / 'harness/sessions')):
        raise Conflict('Harness materialized log path escapes its owned root')
    stamp = hashlib.sha256()
    remaining = manifest['jsonl']['prefix_bytes']
    with path.open('rb') as stream:
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise Conflict('Harness materialized log lost its committed prefix')
            stamp.update(block)
            remaining -= len(block)
    if stamp.hexdigest() != manifest['jsonl']['prefix_sha256']:
        raise Conflict('Harness materialized JSONL prefix hash mismatch')
    return manifest


async def materialize_legacy(runtime, *, process_factory=ImportProcess, page_size=200):
    """Finish every frozen legacy import before starting any model supervision."""
    if type(page_size) is not int or not 1 <= page_size <= 500:
        raise ValueError('Invalid Harness materialization page size')
    store = runtime.store
    with store.transaction() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS harness_materialized(session_id TEXT PRIMARY KEY,
            history_sha256 TEXT NOT NULL,event_count INTEGER NOT NULL,event_sha256 TEXT NOT NULL,
            manifest TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,created TEXT NOT NULL)''')
    reports = []
    records = store.rows('SELECT * FROM harness_migrations ORDER BY created,session_id')
    for record in records:
        history, source = validate_migration(record)
        registered = store.rows('SELECT * FROM harness_materialized WHERE session_id=?', (record['session_id'],))
        if registered:
            reports.append(_verify_registered(store, registered[0], source))
            continue
        process = process_factory(runtime, record['session_id'])
        events = []
        try:
            await process.start({'model': 'legacy-import', 'protocol': 'openai_chat_completions',
                'profile': {'safety_margin_tokens': 2048, 'output_reserve': 2048, 'context_window': 65536},
                'system': '旧会话只读导入；禁止模型请求、工具执行及任务调度。', 'history': history})
            after, total = 0, None
            while True:
                page = await process.call('history_events', {'after': after, 'limit': page_size}, timeout=45)
                if page.get('session_id') != record['session_id'] or type(page.get('total')) is not int:
                    raise Conflict('Harness import event page has a different session identity')
                if total is not None and page['total'] != total:
                    raise Conflict('Harness history changed during import-only inspection')
                total = page['total']
                batch = page['items']
                if not isinstance(batch, list) or len(batch) > page_size:
                    raise Conflict('Invalid Harness import event page')
                for event in batch:
                    if type(event.get('seq')) is not int or event['seq'] != len(events) or not isinstance(event.get('data'), dict):
                        raise Conflict('Harness import event sequence is incomplete or inconsistent')
                    events.append(event)
                expected = events[-1]['seq'] + 1 if events else 0
                if page.get('next_cursor') != expected or (not batch and expected < total):
                    raise Conflict('Harness import event cursor did not advance')
                after = expected
                if after == total:
                    break
                if after > total:
                    raise Conflict('Harness import event count exceeds its durable boundary')
            validate_import(history, events)
            if getattr(process, 'callbacks', []):
                raise Conflict('Import-only Harness attempted model/tool execution')
        finally:
            await process.close()
        physical = _physical_record(store, record['session_id'], events)
        mapping = _run_mapping(store, record['session_id'], history)
        hashes = [digest(event) for event in events]
        # Admit only verified physical records. A crash between batches resumes
        # through immutable INSERT-or-compare checks, never by replacing events.
        for offset in range(0, len(events), page_size):
            with store.transaction() as db:
                for event, stamp in zip(events[offset:offset + page_size], hashes[offset:offset + page_size], strict=True):
                    existing = db.execute('SELECT payload,sha256 FROM harness_events WHERE session_id=? AND seq=?',
                                          (record['session_id'], event['seq'])).fetchone()
                    if existing and (existing['sha256'] != stamp or digest(json.loads(existing['payload'])) != stamp):
                        raise Conflict('Existing Harness event identity has different content')
                    db.execute('INSERT OR IGNORE INTO harness_events(session_id,seq,run_id,payload,sha256) VALUES(?,?,?,?,?)',
                        (record['session_id'], event['seq'], _event_run(event, mapping), canonical(event), stamp))
        manifest = {'version': VERSION, 'session_id': record['session_id'], 'history_sha256': source['history_sha256'],
            'snapshot_sha256': source.get('snapshot_sha256'), 'message_count': len(history), 'event_count': len(events),
            'event_hashes': hashes, 'event_sha256': digest(hashes), 'jsonl': physical, 'materialized': True,
            'model_requests': 0, 'tool_calls': 0}
        with store.transaction() as db:
            db.execute('INSERT INTO harness_materialized VALUES(?,?,?,?,?,?,?)',
                (record['session_id'], source['history_sha256'], len(events), digest(hashes), canonical(manifest), digest(manifest), now()))
        reports.append(manifest)
        _write_report(store, len(records), reports)
    if records:
        _write_report(store, len(records), reports)
    return {'session_count': len(records), 'materialized_count': len(reports), 'sessions': reports}


def _write_report(store, total, reports):
    root = require_f_path(store.root / 'harness/legacy')
    root.mkdir(parents=True, exist_ok=True)
    _atomic_json(root / 'materialization-v1.json', {'version': VERSION,
        'session_count': total, 'materialized_count': len(reports), 'sessions': reports})
