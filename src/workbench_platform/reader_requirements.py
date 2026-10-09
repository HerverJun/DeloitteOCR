"""Persist reader capabilities and infer them from actual records without migrations.

Admission and cold backup readers use this module without constructing services.
Historical snapshots are checked for identity/integrity, not asserted to be current.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

FORMAT = 'clear-product-reader-requirements-v1'
SPECIALIZED_FEATURES = ('agent-preferences-v1', 'screen-delivery-snapshot-v1', 'screen-resolution-v1',
                        'screen-local-candidates-v1', 'screen-business-metadata-edit-v1')
PREFERENCE_TABLES = {
    'agent_preference_profiles': ('workspace_id', 'business', 'revision', 'body', 'updated'),
    'agent_preference_snapshots': ('run_id', 'business', 'workspace_id', 'session_id', 'body', 'sha256', 'created'),
    'agent_preference_requests': ('request_id', 'fingerprint', 'response'),
}
HEX = re.compile(r'^[0-9a-f]{64}$')
_marker_lock = threading.RLock()


def safe_root(value):
    requested = Path(value).absolute()
    if requested.drive.upper() != 'F:' or requested.resolve().drive.upper() != 'F:':
        raise ValueError('Reader requirements must remain on local F drive')
    for path in (requested, *requested.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Linked reader-requirement paths are not allowed')
    return requested


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def value_hash(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def marker_requirements(root):
    marker = safe_root(Path(root) / 'product-reader-requirements.json')
    if not marker.exists():
        return {}
    value = json.loads(marker.read_text('utf-8-sig'))
    if (not isinstance(value, dict) or value.get('format') != FORMAT
            or not isinstance(value.get('reader_features'), list)
            or any(not isinstance(feature, str) or not feature for feature in value['reader_features'])
            or len(set(value['reader_features'])) != len(value['reader_features'])):
        raise ValueError('Stored product reader requirements are invalid')
    return value


def mark_reader_feature(root, feature):
    """Call after durable business write; merge only the marker, never mutate DBs."""
    if feature not in (*SPECIALIZED_FEATURES, 'screen-batch-v1'):
        raise ValueError('Unregistered reader feature')
    root = safe_root(root)
    if not root.is_dir():
        raise ValueError('Reader-requirement root must already exist')
    with _marker_lock:
        previous = marker_requirements(root)
        if feature in previous.get('reader_features', []):
            return previous
        value = {**previous, 'format': FORMAT,
                 'reader_features': sorted({*previous.get('reader_features', []), feature})}
        marker = root / 'product-reader-requirements.json'
        temporary = safe_root(root / ('.reader-requirements-' + uuid.uuid4().hex + '.tmp'))
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
        finally:
            if temporary.exists():
                temporary.unlink()
        return value


def read_database(path):
    path = safe_root(path)
    wal = safe_root(path.with_name(path.name + '-wal'))
    options = '?mode=ro' if wal.is_file() and wal.stat().st_size else '?mode=ro&immutable=1'
    db = sqlite3.connect(path.as_uri() + options, uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def table_columns(db, table):
    # Every caller supplies a code-owned table name.
    return {row['name'] for row in db.execute('PRAGMA table_info("' + table + '")')}


def require_columns(db, table, required):
    columns = table_columns(db, table)
    if not set(required) <= columns:
        raise ValueError('Specialized data structure is incompatible: ' + table)


def encoded_object(value, label):
    try:
        result = json.loads(value)
    except (TypeError, ValueError) as error:
        raise ValueError('Invalid specialized JSON: ' + label) from error
    if not isinstance(result, dict):
        raise ValueError('Specialized record must be an object: ' + label)
    return result


def preference_entries(db, entries, workspace, business, maximum_revision, *, allow_project=True):
    from .agent.preferences import PreferenceSource, checked_values
    if not isinstance(entries, dict):
        raise ValueError('Preference entries must be objects')
    for key, item in entries.items():
        bound = maximum_revision.get(item.get('business'), -1) if isinstance(maximum_revision, dict) and isinstance(item, dict) else maximum_revision
        if (not isinstance(item, dict) or item.get('workspace_id') != workspace
                or item.get('business') not in (('project', business) if allow_project else (business,))
                or type(item.get('revision')) is not int or not 1 <= item['revision'] <= bound):
            raise ValueError('Preference entry lost project/business/revision identity')
        checked_values({key: item.get('value')})
        source = item.get('source') or {}
        PreferenceSource.model_validate({name: source.get(name) for name in ('mode', 'quote', 'event_seq')})
        if source['mode'] == 'user_form':
            if source.get('actor') != 'user' or source.get('event_seq') is not None:
                raise ValueError('Preference form origin differs')
        else:
            if source.get('actor') != 'agent_from_user_instruction' or not source.get('session_id'):
                raise ValueError('Preference user-event origin differs')
            require_columns(db, 'events', ('seq', 'session_id', 'kind', 'payload'))
            require_columns(db, 'sessions', ('id', 'workspace_id'))
            event = db.execute('SELECT * FROM events WHERE seq=? AND session_id=?',
                               (source['event_seq'], source['session_id'])).fetchone()
            session = db.execute('SELECT workspace_id FROM sessions WHERE id=?', (source['session_id'],)).fetchone()
            payload = encoded_object(event['payload'], 'preference event') if event else {}
            if (not session or session['workspace_id'] != workspace or not event or event['kind'] != 'message'
                    or payload.get('role') != 'user' or source['quote'] not in str(payload.get('content', ''))
                    or not re.search(r'默认|以后|今后|记住|偏好|总是|日常|default|remember|always', source['quote'], re.I)):
                raise ValueError('Preference origin is not its actual project user event')


def validate_preferences(db, present):
    from .agent.preferences import TaskPreferences, checked_values
    for table, columns in PREFERENCE_TABLES.items():
        if table in present:
            require_columns(db, table, columns)
    if 'agent_preference_profiles' in present:
        for row in db.execute('SELECT * FROM agent_preference_profiles'):
            TaskPreferences._business(row['business'])
            if not row['workspace_id'] or type(row['revision']) is not int or row['revision'] < 1:
                raise ValueError('Preference profile identity/revision is invalid')
            entries = encoded_object(row['body'], 'preference profile')
            preference_entries(db, entries, row['workspace_id'], row['business'], row['revision'], allow_project=False)
    if 'agent_preference_requests' in present:
        for row in db.execute('SELECT * FROM agent_preference_requests'):
            value = encoded_object(row['response'], 'preference request receipt')
            TaskPreferences._business(value.get('business'))
            if (not row['request_id'] or not HEX.fullmatch(str(row['fingerprint']))
                    or not value.get('workspace_id') or type(value.get('revision')) is not int or value['revision'] < 1
                    or value.get('defaults_apply_to_future_runs') is not True
                    or not isinstance(value.get('revoked_keys'), list)):
                raise ValueError('Preference request receipt identity is invalid')
            require_columns(db, 'agent_preference_profiles', PREFERENCE_TABLES['agent_preference_profiles'])
            profile = db.execute('SELECT revision FROM agent_preference_profiles WHERE workspace_id=? AND business=?',
                                 (value['workspace_id'], value['business'])).fetchone()
            if not profile or profile['revision'] < value['revision']:
                raise ValueError('Preference request receipt has no actual project profile')
            preference_entries(db, value.get('entries'), value['workspace_id'], value['business'], value['revision'], allow_project=False)
    if 'agent_preference_snapshots' not in present:
        return
    snapshots = db.execute('SELECT * FROM agent_preference_snapshots').fetchall()
    if not snapshots:
        return
    require_columns(db, 'runs', ('id', 'workspace_id', 'session_id'))
    require_columns(db, 'sessions', ('id', 'workspace_id', 'selected_assets', 'initial_refs'))
    for row in snapshots:
        value = encoded_object(row['body'], 'preference snapshot')
        TaskPreferences._business(row['business'])
        if (type(value.get('version')) is not int or value['version'] != 1 or not HEX.fullmatch(str(row['sha256'])) or value_hash(value) != row['sha256']
                or any(value.get(key) != row[key] for key in ('workspace_id', 'business', 'session_id', 'run_id', 'created'))):
            raise ValueError('Preference snapshot bytes/version/row identity differs')
        run = db.execute('SELECT * FROM runs WHERE id=?', (row['run_id'],)).fetchone()
        session = db.execute('SELECT * FROM sessions WHERE id=?', (row['session_id'],)).fetchone()
        if (not run or not session or run['workspace_id'] != row['workspace_id'] or run['session_id'] != row['session_id']
                or session['workspace_id'] != row['workspace_id']
                or value.get('scope_sha256') != value_hash([session['selected_assets'], session['initial_refs']])):
            raise ValueError('Preference snapshot lost its actual run/session/source scope')
        if any(type(value.get(key)) is not int or value[key] < 0 for key in ('profile_revision', 'project_revision')):
            raise ValueError('Preference snapshot revision is invalid')
        sources = value.get('sources')
        preference_entries(db, sources, row['workspace_id'], row['business'],
                           {'project': value['project_revision'], row['business']: value['profile_revision']})
        overrides = checked_values(value.get('overrides'))
        expected = {**{key: item['value'] for key, item in sources.items()}, **overrides}
        if checked_values(value.get('values')) != expected:
            raise ValueError('Preference snapshot values differ from frozen sources/overrides')


def validate_delivery(db, row):
    value = encoded_object(row['snapshot_json'], 'delivery snapshot')
    if not HEX.fullmatch(str(value.get('snapshot_id'))):
        raise ValueError('Delivery snapshot identity is invalid')
    from .jobs.screen_batches import ScreenExport
    ScreenExport.model_validate(value.get('export_options'))
    require_columns(db, 'screen_batch_refs', ('workspace_id', 'batch_id', 'project_id', 'source_json'))
    source = db.execute('SELECT * FROM screen_batch_refs WHERE workspace_id=? AND batch_id=?',
                        (row['workspace_id'], row['batch_id'])).fetchone()
    if not source:
        raise ValueError('Delivery snapshot has no actual project batch source')
    # Minimal v1 stored only the content digest and options. A full frozen
    # vector, when present, must also match the actual persisted source scope.
    vector = value.get('version_vector')
    if vector is not None:
        sources = json.loads(source['source_json'])
        identity = {key: vector.get(key) for key in ('batch_id', 'project_id', 'policy_sha256', 'items')}
        items = vector.get('items')
        if (vector.get('snapshot_id') != value['snapshot_id'] or vector.get('export_options') != value['export_options']
                or vector.get('batch_id') != row['batch_id'] or vector.get('project_id') != source['project_id']
                or not isinstance(items, list) or len(items) != len(sources)
                or {item.get('input_version_id'): item.get('image_id') for item in items}
                    != {item.get('input_version_id'): item.get('image_id') for item in sources}
                or value_hash(identity) != value['snapshot_id']):
            raise ValueError('Delivery snapshot vector lost its batch/source identity')


def validate_resolution(db, row, value):
    if (value.get('resolution_version') != 'screen-resolution-v1'
            or value.get('status') not in ('intent', 'submitted', 'blocked', 'reused')
            or value.get('parent_batch_id') != row['batch_id']
            or value.get('values_modified') is not False or value.get('selection_applied') is not False
            or value.get('human_review') != 'not_performed'):
        raise ValueError('Resolution version/parent/authority identity differs')
    require_columns(db, 'screen_batches', ('id', 'project_id'))
    parent = db.execute('SELECT project_id FROM screen_batches WHERE id=?', (row['batch_id'],)).fetchone()
    if not parent:
        raise ValueError('Resolution parent batch is missing')
    bindings = value.get('bindings', [])
    if value['status'] in ('intent', 'submitted', 'reused') and not bindings:
        raise ValueError('Resolution intent/candidate has no frozen sources')
    for binding in bindings:
        require_columns(db, 'versions', ('id', 'image_id', 'sha256'))
        require_columns(db, 'results', ('id', 'task_id', 'revision'))
        require_columns(db, 'tasks', ('id', 'version_id', 'image_id', 'project_id'))
        version = db.execute('SELECT * FROM versions WHERE id=?', (binding.get('input_version_id'),)).fetchone()
        result = db.execute('''SELECT r.revision,t.version_id,t.image_id,t.project_id FROM results r
            JOIN tasks t ON t.id=r.task_id WHERE r.id=?''', (binding.get('result_id'),)).fetchone()
        if (not version or not result or binding.get('source_sha256') != version['sha256']
                or result['version_id'] != version['id'] or result['image_id'] != version['image_id']
                or result['project_id'] != parent['project_id']
                or type(binding.get('result_revision')) is not int or not 0 <= binding['result_revision'] <= result['revision']
                or any(not HEX.fullmatch(str(binding.get(key))) for key in ('source_sha256', 'table_snapshot_sha256', 'problem_signature'))):
            raise ValueError('Resolution binding differs from actual immutable source/result ownership')
    if value['status'] == 'intent':
        submit = value.get('submit_body') or {}
        if submit.get('version_ids') != [item['input_version_id'] for item in bindings] or submit.get('policy') != value.get('policy'):
            raise ValueError('Resolution frozen submission differs from its binding/policy')


def infer_reader_requirements(root):
    """Read real records, including when their marker was lost or omitted."""
    root = safe_root(root)
    features, evidence = set(), []
    paths = [('agent/operations.sqlite3', 'preferences'), ('screen-batches.sqlite3', 'delivery'),
             ('native/ocr/workbench.sqlite3', 'resolution'), ('workbench.sqlite3', 'resolution')]
    for relative, kind in paths:
        path = safe_root(root / relative)
        if not path.is_file():
            continue
        with closing(read_database(path)) as db:
            db.execute('BEGIN')
            present = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if kind == 'preferences':
                validate_preferences(db, present)
                for table in PREFERENCE_TABLES:
                    if table in present and db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                        features.add('agent-preferences-v1')
                        evidence.append({'database': relative, 'table': table, 'feature': 'agent-preferences-v1'})
            elif kind == 'delivery':
                if 'screen_local_request_refs' in present:
                    require_columns(db, 'screen_local_request_refs', ('workspace_id', 'request_id', 'batch_id',
                        'body_json', 'task_ids_json', 'owned_task_ids_json', 'created'))
                    from .jobs.screen_local_candidates import ScreenLocalSubmit
                    for row in db.execute('SELECT * FROM screen_local_request_refs'):
                        body = ScreenLocalSubmit.model_validate(encoded_object(row['body_json'], 'local candidate intent'))
                        require_columns(db, 'screen_batch_refs', ('workspace_id', 'batch_id', 'source_json'))
                        parent = db.execute('SELECT * FROM screen_batch_refs WHERE workspace_id=? AND batch_id=?',
                            (row['workspace_id'], row['batch_id'])).fetchone()
                        if (body.request_id != row['request_id'] or not parent
                                or body.target.input_version_id not in {item['input_version_id']
                                    for item in json.loads(parent['source_json'])}):
                            raise ValueError('Local candidate intent lost its original source scope')
                        from .jobs.screen_local_candidates import _identifiers
                        if row['task_ids_json'] is not None:
                            tasks = json.loads(row['task_ids_json'])
                            owned = json.loads(row['owned_task_ids_json'])
                            if not _identifiers(tasks) or not _identifiers(owned) or not set(owned) <= set(tasks):
                                raise ValueError('Local candidate task identities are invalid')
                        features.add('screen-local-candidates-v1')
                        evidence.append({'database': relative, 'table': 'screen_local_request_refs',
                            'feature': 'screen-local-candidates-v1', 'request_id': row['request_id']})
                if 'screen_batch_refs' in present and db.execute('SELECT 1 FROM screen_batch_refs LIMIT 1').fetchone():
                    features.add('screen-batch-v1')
                if 'screen_batch_exports' in present and 'snapshot_json' in table_columns(db, 'screen_batch_exports'):
                    require_columns(db, 'screen_batch_exports', ('workspace_id', 'batch_id', 'bundle_id', 'snapshot_json'))
                    for row in db.execute("SELECT * FROM screen_batch_exports WHERE snapshot_json IS NOT NULL AND snapshot_json<>''"):
                        validate_delivery(db, row)
                        features.add('screen-delivery-snapshot-v1')
                        evidence.append({'database': relative, 'table': 'screen_batch_exports', 'feature': 'screen-delivery-snapshot-v1', 'bundle_id': row['bundle_id']})
            elif 'screen_actions' in present:
                require_columns(db, 'screen_actions', ('batch_id', 'request_id', 'receipt'))
                for row in db.execute('SELECT * FROM screen_actions'):
                    value = encoded_object(row['receipt'], 'screen action')
                    if 'resolution_version' in value:
                        validate_resolution(db, row, value)
                        features.add('screen-resolution-v1')
                        evidence.append({'database': relative, 'table': 'screen_actions', 'feature': 'screen-resolution-v1', 'request_id': row['request_id']})
                if 'screen_local_requests' in present:
                    require_columns(db, 'screen_local_requests', ('task_id', 'batch_id', 'root_batch_id', 'request_id',
                        'input_version_id', 'parent_result_id', 'pixel_key', 'snapshot', 'metadata', 'candidate_state'))
                    require_columns(db, 'tasks', ('id', 'project_id', 'input_version_id', 'image_id', 'kind', 'engine'))
                    require_columns(db, 'versions', ('id', 'image_id', 'sha256'))
                    require_columns(db, 'results', ('id', 'task_id', 'revision'))
                    require_columns(db, 'screen_batches', ('id', 'project_id'))
                    for row in db.execute('SELECT * FROM screen_local_requests'):
                        snapshot = encoded_object(row['snapshot'], 'local candidate snapshot')
                        task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
                        source = db.execute('SELECT * FROM versions WHERE id=?', (row['input_version_id'],)).fetchone()
                        result = db.execute('SELECT * FROM results WHERE id=?', (row['parent_result_id'],)).fetchone()
                        parent = db.execute('SELECT * FROM screen_batches WHERE id=?', (row['batch_id'],)).fetchone()
                        root_batch = db.execute('SELECT * FROM screen_batches WHERE id=?', (row['root_batch_id'],)).fetchone()
                        result_task = db.execute('SELECT * FROM tasks WHERE id=?', (result['task_id'],)).fetchone() if result else None
                        if (not task or not source or not result or not result_task or not parent or not root_batch
                                or task['kind'] != 'screen_local_text' or task['engine'] != 'glm'
                                or task['project_id'] != parent['project_id'] or root_batch['project_id'] != parent['project_id']
                                or result_task['project_id'] != parent['project_id']
                                or task['input_version_id'] != source['id'] or task['image_id'] != source['image_id']
                                or result_task['image_id'] != source['image_id']
                                or snapshot.get('input_version_id') != source['id']
                                or snapshot.get('result_id') != result['id'] or snapshot.get('batch_id') != row['batch_id']
                                or type(snapshot.get('result_revision')) is not int
                                or not 0 <= snapshot['result_revision'] <= result['revision']
                                or snapshot.get('source_sha256') != source['sha256']
                                or not HEX.fullmatch(str(row['pixel_key']))):
                            raise ValueError('Local candidate source/result/task ownership is invalid')
                        features.add('screen-local-candidates-v1')
                        evidence.append({'database': relative, 'table': 'screen_local_requests',
                            'feature': 'screen-local-candidates-v1', 'task_id': row['task_id']})
            if kind == 'resolution' and 'results' in present and 'edited' in table_columns(db, 'results'):
                for row in db.execute('SELECT * FROM results'):
                    edited = encoded_object(row['edited'], 'OCR current edit')
                    if 'screen' not in edited:
                        continue
                    require_columns(db, 'results', ('id', 'original', 'revision', 'task_id'))
                    original = encoded_object(row['original'], 'OCR original result')
                    screen = edited['screen']
                    if (original.get('origin') != 'screen_batch' or not isinstance(screen, dict)
                            or set(screen) != {'business_metadata', 'metadata_edit_log', 'metadata_review'}
                            or not isinstance(screen['business_metadata'], list)
                            or not isinstance(screen['metadata_edit_log'], list)
                            or not isinstance(screen['metadata_review'], dict)):
                        raise ValueError('Invalid screen business metadata edit structure')
                    review = screen['metadata_review']
                    if (review.get('actor') != 'human' or review.get('state') not in {'accepted','retain','deferred'}
                            or type(review.get('base_revision')) is not int
                            or not 0 <= review['base_revision'] < row['revision']
                            or review.get('input_version_id') != original.get('project_image_version')):
                        raise ValueError('Business metadata review lost its input/revision source')
                    require_columns(db, 'tasks', ('id', 'version_id', 'image_id'))
                    task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
                    if not task or task['version_id'] != review['input_version_id']:
                        raise ValueError('Business metadata task source differs')
                    for part in screen['business_metadata']:
                        if (not isinstance(part, dict) or not isinstance(part.get('text'), str)
                                or not isinstance(part.get('kind'), str)
                                or type(part.get('human_confirmed')) is not bool
                                or type(part.get('review_required')) is not bool):
                            raise ValueError('Invalid business metadata current value')
                    for entry in screen['metadata_edit_log']:
                        if (not isinstance(entry, dict) or entry.get('actor') != 'human'
                                or entry.get('image_version') != review['input_version_id']
                                or type(entry.get('base_revision')) is not int
                                or not 0 <= entry['base_revision'] <= review['base_revision']
                                or type(entry.get('item_index')) is not int
                                or not 0 <= entry['item_index'] < len(screen['business_metadata'])
                                or entry.get('choice') not in {'manual','primary','auxiliary'}):
                            raise ValueError('Business metadata history lost its source binding')
                    features.add('screen-business-metadata-edit-v1')
                    evidence.append({'database': relative, 'table': 'results', 'result_id': row['id'],
                        'feature': 'screen-business-metadata-edit-v1'})
    return {'reader_features': sorted(features), 'evidence': evidence, 'writes': 0}


def ensure_reader_requirements(root, *, create_missing=False):
    """Validate backup requirements; writes are allowed only in its private stage."""
    inferred = infer_reader_requirements(root)
    previous = marker_requirements(root)
    missing = set(inferred['reader_features']) - set(previous.get('reader_features', []))
    if missing and not create_missing:
        raise ValueError('Backup is missing actual-record reader capabilities: ' + ','.join(sorted(missing)))
    for feature in sorted(missing):
        mark_reader_feature(root, feature)
    return inferred
