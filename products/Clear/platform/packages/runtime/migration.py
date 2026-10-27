"""Versioned relocation of operational references, never business evidence.

Callers operate on a consistent backup in a new DATA directory and own the
transaction. Original request/result JSON, model bindings, hashes, revisions and
audit/history records are deliberately outside these field maps.
"""
from __future__ import annotations

from copy import deepcopy
import json
import ntpath
from pathlib import PureWindowsPath

MAPPING_VERSION = 1

# A path-like string is insufficient authorization to edit a business value.
# Paths below are structural field paths; '*' applies only to list members.
_PATH_FIELDS = {
    'command': (
        ('body_file',), ('response_file',), ('request_file',),
        ('native', 'body_file'), ('native', 'response_file'),
        ('payload', 'session_dir'), ('payload', 'input_path'),
        ('payload', 'output_dir'), ('payload', 'output_path'),
    ),
    'receipt': (
        ('response_file',), ('output_path',), ('output_dir',), ('file_path',),
        ('artifacts', '*', 'path'), ('files', '*', 'path'),
        ('result', 'response_file'), ('result', 'output_path'),
    ),
    'request': (
        ('session_dir',), ('path',), ('input_path',), ('output',),
        ('image_output',), ('output_dir',), ('output_path',), ('response_path',),
        ('inputs', '*', 'path'), ('files', '*', 'path'),
    ),
    'live-job': (
        ('output_path',), ('output_dir',), ('input_path',),
        ('inputs', '*', 'path'), ('files', '*', 'path'),
        ('pages', '*', 'image_path'), ('pages', '*', 'path'),
        ('pages', '*', 'raw_path'), ('pages', '*', 'output_dir'),
    ),
}


def relocate_path(value, old, new, external_map=None):
    """Relocate an exact local path prefix, respecting Windows case/boundaries."""
    if not isinstance(value, str) or not value:
        return value
    if not PureWindowsPath(value).is_absolute():
        return value  # Includes HTTP routes and package-relative members.
    if '..' in PureWindowsPath(value).parts:
        return value  # Ambiguous historical references require human review.
    for source, relative in (external_map or {}).items():
        if ntpath.normcase(ntpath.normpath(value)) == ntpath.normcase(ntpath.normpath(source)):
            member = PureWindowsPath(relative)
            if member.is_absolute() or member.drive or '..' in member.parts:
                raise ValueError('External backup mapping must be a relative DATA member')
            return str(PureWindowsPath(new) / member)
    source, target = PureWindowsPath(old), PureWindowsPath(new)
    if not source.is_absolute() or not target.is_absolute():
        raise ValueError('DATA relocation requires absolute Windows roots')
    path = PureWindowsPath(value)
    try:
        relative = path.relative_to(source)
    except ValueError:
        return value
    return str(target / relative)


def _transform(value, fields, old, new, external_map):
    result = deepcopy(value)

    def apply(current, pattern):
        if not pattern:
            return relocate_path(current, old, new, external_map)
        head, *tail = pattern
        if head == '*':
            if isinstance(current, list):
                return [apply(item, tail) for item in current]
        elif isinstance(current, dict) and head in current:
            current[head] = apply(current[head], tail)
        return current

    for pattern in fields:
        result = apply(result, pattern)
    return result


def relocate_operational(value, old, new, *, context='command', external_map=None):
    if context not in _PATH_FIELDS:
        raise ValueError('Unknown operational relocation context: ' + context)
    return _transform(value, _PATH_FIELDS[context], old, new, external_map)


# Table signatures disambiguate native documents from platform documents and
# reject future native schemas before any mutation. Extra tables are preserved.
_COLUMNS = (
    ('images', 'id', 'original_path', 'ocr', 22),
    ('versions', 'id', 'path', 'ocr', 22),
    ('documents', 'id', 'original_path', 'ocr', 22),
    ('pages', 'id', 'native_result', 'ocr', 22),
    ('screen_tasks', 'task_id', 'prepared_path', 'ocr', 22),
    ('assets', 'id', 'path', 'classification', 5),
    ('datasets', 'id', 'manifest_path', 'classification', 5),
    ('models', 'id', 'package_path', 'classification', 5),
    ('job_items', 'id', 'path', 'classification', 5),
    ('derived_inputs', 'asset_id', 'provenance_path', 'agent', 0),
)
_JSON = (
    ('operation_requests', 'request_key', 'request', 'request', 0),
    ('command_receipts', 'dispatch_id', 'receipt', 'receipt', 0),
    ('documents', 'document_id', 'body', 'live-job', 1),
    ('comparison_jobs', 'id', 'body', 'live-job', 1),
    ('manual_analysis', 'job_id', 'body', 'live-job', 0),
    ('inspections', 'inspection_id', 'body', 'live-job', 1),
)
_ACTIVE = {'queued', 'running', 'loading', 'cancelling', 'pausing', 'paused',
           'waiting', 'waiting_job', 'waiting_user', 'needs_verification'}


def relocate_sqlite(db, old, new, external_map=None):
    """Update known operational fields and interrupt restored work for recheck.

    This does not activate a new default resource or rewrite a frozen snapshot.
    Resource qualification and account-specific secret initialization remain the
    launcher's responsibility. Unknown/future schemas fail before any mutation.
    """
    relocate_path(str(PureWindowsPath(old)), old, new, external_map)
    version = db.execute('PRAGMA user_version').fetchone()[0]
    tables = {row[0]: {column[0] for column in db.execute('SELECT name FROM pragma_table_info(?)', (row[0],))}
              for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
    candidates = [item for item in _COLUMNS if {item[1], item[2]} <= tables.get(item[0], set())]
    profiles = {
        'ocr': {'images', 'versions', 'tasks', 'results'} <= tables.keys(),
        'classification': {'assets', 'datasets', 'models', 'jobs'} <= tables.keys(),
        'agent': {'sessions', 'runs', 'events', 'operations'} <= tables.keys(),
    }
    selected_columns = [item for item in candidates if profiles[item[3]]]
    selected_json = [item for item in _JSON if {item[1], item[2]} <= tables.get(item[0], set())]
    for table, identity, column, profile, maximum in candidates:
        if version > maximum:
            raise ValueError(f'Unsupported {profile} SQLite version {version}; table {table} was not relocated')
    for table, identity, column, context, maximum in selected_json:
        if version > maximum:
            raise ValueError(f'Unsupported operational SQLite version {version}; table {table} was not relocated')
    report = {'mapping_version': MAPPING_VERSION, 'database_version': version,
              'updated_fields': {}, 'interrupted_jobs': 0, 'cleared_leases': 0,
              'profiles': [name for name, recognized in profiles.items() if recognized],
              'preserved_tables': sorted(tables)}
    # Pre-decode every selected JSON before making changes, so malformed legacy
    # bodies cannot leave a partially relocated database even without a savepoint.
    prepared = []
    for table, identity, column, context, _ in selected_json:
        for key, raw in db.execute(f'SELECT "{identity}","{column}" FROM "{table}" WHERE "{column}" IS NOT NULL').fetchall():
            value = json.loads(raw)
            moved = relocate_operational(value, old, new, context=context, external_map=external_map)
            if context == 'live-job' and isinstance(moved, dict) and moved.get('status') in _ACTIVE:
                moved.update(status='needs_verification', stage='interrupted',
                    error='安装已迁移；请重新验证原固定资源后明确恢复，历史成果仍可读取')
                report['interrupted_jobs'] += 1
            if moved != value:
                prepared.append((table, identity, column, key, json.dumps(moved, ensure_ascii=False, separators=(',', ':'))))
    for table, identity, column, _, _ in selected_columns:
        for key, value in db.execute(f'SELECT "{identity}","{column}" FROM "{table}" WHERE "{column}" IS NOT NULL').fetchall():
            moved = relocate_path(value, old, new, external_map)
            if moved != value:
                db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{identity}"=?', (moved, key))
                name = table + '.' + column
                report['updated_fields'][name] = report['updated_fields'].get(name, 0) + 1
    for table, identity, column, key, value in prepared:
        db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{identity}"=?', (value, key))
        name = table + '.' + column
        report['updated_fields'][name] = report['updated_fields'].get(name, 0) + 1
    # Never replay a restored live lease or transplant PID/owner evidence.
    if profiles['ocr'] and {'id', 'status', 'engine'} <= tables.get('tasks', set()) and version <= 22:
        cursor = db.execute("UPDATE tasks SET status='interrupted' WHERE status IN ('queued','running','paused','pausing','cancelling')")
        report['interrupted_jobs'] += cursor.rowcount
    if profiles['classification'] and {'id', 'state', 'lease_token', 'owner', 'lease_until'} <= tables.get('jobs', set()) and version <= 5:
        cursor = db.execute("UPDATE jobs SET state='interrupted',owner=NULL,lease_token=NULL,lease_until=NULL WHERE state IN ('queued','running','paused','pausing','cancelling')")
        report['interrupted_jobs'] += cursor.rowcount
        if {'resource', 'token', 'expires'} <= tables.get('leases', set()):
            report['cleared_leases'] = db.execute('DELETE FROM leases').rowcount
    if profiles['agent'] and {'id', 'session_id', 'status', 'generation'} <= tables.get('runs', set()) and version == 0:
        cursor = db.execute("UPDATE runs SET status='needs_verification' WHERE status IN ('running','waiting_job','waiting_user')")
        report['interrupted_jobs'] += cursor.rowcount
    return report
