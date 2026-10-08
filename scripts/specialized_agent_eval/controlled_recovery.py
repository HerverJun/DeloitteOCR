"""Prepare a stopped native OCR/Agent scope without starting services or models.

Original SQLite files are copied as bytes, never opened by SQLite. All mutation
is confined to a unique F-drive runtime copy. Launch requires a separately
verified full source snapshot; this module intentionally has no run command.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct

from common import fingerprint, fpath, frozen_json, read_json

FORMAT = 'controlled-native-recovery-prepare-v1'
FOLLOWUP_FORMAT = 'controlled-native-recovery-followup-failure-v1'
NATIVE_DB = 'native/ocr/workbench.sqlite3'
COORDINATE_KEYS = {'width','height','source_to_prepared','prepared_to_source','source_polygon',
                   'source_box','coordinate_space','input_sha256','prepared_pixel_sha256',
                   'business_regions','excluded_regions'}
REQUIRED_SOURCE_FILES = {
    'ocr': {'src/ocr_workbench/service.py','src/ocr_workbench/screen_batches.py',
            'src/ocr_workbench/task_queue.py','src/ocr_workbench/store.py',
            'src/ocr_workbench/adapter.py','src/ocr_workbench/worker.py',
            'src/ocr_workbench/hunyuan.py','config/screen-policy.json'},
    'platform': {'src/workbench_platform/agent/runtime.py',
                 'src/workbench_platform/jobs/screen_batches.py',
                 'src/workbench_platform/agent/tools/screen_batches.py'}
}


def file_sha(path):
    value = hashlib.sha256()
    with fpath(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def bound(path):
    path = fpath(path)
    return {'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}


def plain_path(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink() or part.is_junction():
            raise ValueError('Recovery paths cannot traverse links or junctions')
    return fpath(path)


def file_inventory(root):
    root = plain_path(root)
    answer = {}
    for path in sorted(root.rglob('*')):
        plain_path(path)
        if path.is_file():
            answer[path.relative_to(root).as_posix()] = {'sha256': file_sha(path), 'bytes': path.stat().st_size}
    return answer


def directory_inventory(root):
    root = plain_path(root)
    return [path.relative_to(root).as_posix() for path in sorted(root.rglob('*'))
            if plain_path(path).is_dir()]


def require_environment():
    for key in ('TEMP','TMP','LOCALAPPDATA','APPDATA','HF_HOME','TORCH_HOME','XDG_CACHE_HOME'):
        if not os.environ.get(key):
            raise ValueError('Explicit F-drive subprocess cache/temp environment is required')
        plain_path(os.environ[key])


def copy_bytes(source, target, expected, expected_directories=None):
    source, target = plain_path(source), plain_path(target)
    directories = directory_inventory(source)
    if expected_directories is not None and directories != expected_directories:
        raise ValueError('Original directory structure differs from its frozen inventory')
    target.mkdir(parents=True, exist_ok=False)
    for name in directories:
        output = plain_path(target / name)
        if not output.is_relative_to(target):
            raise ValueError('Copy directory escapes recovery directory')
        output.mkdir(parents=True, exist_ok=True)
    for name, metadata in expected.items():
        output = plain_path(target / name)
        if not output.is_relative_to(target):
            raise ValueError('Copy member escapes recovery directory')
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, output)
        if file_sha(output) != metadata['sha256']:
            raise ValueError('Copied bytes differ from the frozen original inventory')
    if file_inventory(target) != expected:
        raise ValueError('Recovery byte-copy inventory differs')
    if directory_inventory(target) != directories or directory_inventory(source) != directories:
        raise ValueError('Recovery byte-copy lost or changed original directories')


def is_original_reference(value, original):
    normalize = lambda text: re.sub('/+', '/', text.replace('\\','/')).casefold()
    return normalize(str(original)) in normalize(value)


def remap_value(value, original, runtime):
    if isinstance(value, dict):
        return {key: remap_value(item, original, runtime) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_value(item, original, runtime) for item in value]
    if not isinstance(value, str) or not is_original_reference(value, original):
        return value
    normalized = value.replace('\\','/')
    prefix = str(original).replace('\\','/')
    if normalized.casefold() != prefix.casefold() and not normalized.casefold().startswith(prefix.casefold() + '/'):
        raise ValueError('Opaque text contains an original-scope path; cannot safely migrate it')
    suffix = normalized[len(prefix):].lstrip('/')
    destination = plain_path(runtime / suffix)
    if not destination.is_relative_to(runtime):
        raise ValueError('Mapped runtime path escapes recovery scope')
    if not destination.exists():
        raise ValueError('Mapped original-scope path has no actual copied target')
    return str(destination)


def rewrite_text(value, original, runtime):
    if not isinstance(value, str) or not is_original_reference(value, original):
        return value
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return remap_value(value, original, runtime)
    changed = remap_value(parsed, original, runtime)
    return json.dumps(changed, ensure_ascii=False, separators=(',',':'))


def coordinates(value):
    if isinstance(value, dict):
        return {key: deepcopy(item) if key in COORDINATE_KEYS else coordinates(item)
                for key, item in value.items() if key in COORDINATE_KEYS or isinstance(item, (dict,list))}
    if isinstance(value, list):
        return [coordinates(item) for item in value]
    return None


def quote(name):
    return '"' + name.replace('"','""') + '"'


def connect(path, *, readonly=False):
    path = plain_path(path)
    value = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) if readonly else sqlite3.connect(path)
    value.row_factory = sqlite3.Row
    value.execute('PRAGMA temp_store=MEMORY')
    if readonly:
        value.execute('PRAGMA query_only=ON')
    return value


def consolidate_database(path, runtime):
    path = plain_path(path)
    if not path.is_relative_to(runtime):
        raise ValueError('SQLite consolidation can only modify the new runtime copy')
    temporary = plain_path(path.with_name(path.name + '.consistent-copy'))
    if temporary.exists():
        raise ValueError('Consistent database output already exists')
    original_copy_binding = bound(path)
    with connect(path, readonly=True) as source:
        with connect(temporary) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Copied SQLite logical snapshot failed integrity check')
        target.close()
    source.close()
    for sidecar in (path.with_name(path.name+'-wal'),path.with_name(path.name+'-shm')):
        if sidecar.exists():
            plain_path(sidecar).unlink()
    path.unlink()
    temporary.replace(path)
    return {'byte_copy_database': original_copy_binding, 'consistent_database': bound(path)}


def migrate_database_paths(path, original, runtime):
    changes = []
    connection = connect(path)
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = [row[1] for row in connection.execute('PRAGMA table_info('+quote(table)+')')]
            records = list(connection.execute('SELECT rowid AS recovery_rowid,* FROM '+quote(table)))
            for row in records:
                for column in columns:
                    value = row[column]
                    if isinstance(value, bytes):
                        prefixes = [str(original).encode(), str(original).replace('\\','/').encode()]
                        if any(prefix.lower() in value.lower() for prefix in prefixes):
                            raise ValueError('Opaque database binary contains a runtime path; explicit review is required')
                        continue
                    changed = rewrite_text(value, original, runtime)
                    if changed == value:
                        continue
                    try:
                        old_coordinates, new_coordinates = coordinates(json.loads(value)), coordinates(json.loads(changed))
                    except (ValueError, TypeError):
                        old_coordinates = new_coordinates = None
                    if old_coordinates != new_coordinates:
                        raise ValueError('Path migration changed original image geometry')
                    connection.execute('UPDATE '+quote(table)+' SET '+quote(column)+'=? WHERE rowid=?', (changed,row['recovery_rowid']))
                    changes.append({'database': path.relative_to(runtime).as_posix(), 'table': table,
                        'rowid': row['recovery_rowid'], 'column': column,
                        'before_sha256': hashlib.sha256(str(value).encode()).hexdigest(),
                        'after_sha256': hashlib.sha256(str(changed).encode()).hexdigest(),
                        'coordinate_sha256': fingerprint(old_coordinates), 'geometry_unchanged': True})
        connection.commit()
    finally:
        connection.close()
    return changes


def stop_evidence(original, receipt_path, expected):
    receipt_path = plain_path(receipt_path)
    receipt = read_json(receipt_path)
    followup = receipt.get('format') == FOLLOWUP_FORMAT
    if (receipt.get('format') not in {'h-development-failure-readback-v1', FOLLOWUP_FORMAT}
            or plain_path(receipt['original_run']) != original
            or receipt.get('all_original_run_files_unchanged') is not True
            or receipt.get('sqlite_originals_opened') is not False
            or receipt.get('model_invoked') is not False
            or receipt.get('original_recovery_invoked') is not False):
        raise ValueError('Original failure receipt is not a verified immutable readback')
    if read_json(plain_path(receipt['original_inventory_before']['path'])) != expected:
        raise ValueError('Original run differs from its failure-scene inventory')
    if file_sha(receipt['original_inventory_before']['path']) != receipt['original_inventory_before']['sha256']:
        raise ValueError('Original failure inventory binding changed')
    failure = receipt['failure']
    handle = failure['terminal_handle']
    if (handle.get('exit_code') in (None,0) or handle.get('observation_timeout') is not False
            or handle.get('active_restart') is not False or failure.get('matching_owned_python_or_llama_processes') != 0):
        raise ValueError('Original process terminal evidence is incomplete')
    process_snapshot = failure['process_snapshot']
    if file_sha(process_snapshot['path']) != process_snapshot['sha256']:
        raise ValueError('Owned-process snapshot changed')
    snapshot = json.loads(Path(process_snapshot['path']).read_text('utf-8-sig'))
    if snapshot.get('matching_python_or_llama_processes') != []:
        raise ValueError('Original owned processes still exist')
    timeline = receipt['timeline']['file']
    if ((not followup and plain_path(timeline['path']) != original/'stage-timestamps.json')
            or bound(timeline['path']) != timeline):
        raise ValueError('Original cleanup timeline binding changed')
    stages = ([json.loads(line) for line in Path(timeline['path']).read_text('utf-8').splitlines()]
              if followup else read_json(timeline['path'])['events'])
    finished = ([row for row in stages if row['stage']=='cleanup_finished'] if followup else
                [row for row in stages if row['stage']=='cleanup' and row['event']=='finished'])
    if len(finished) != 1:
        raise ValueError('A unique proven original cleanup upper bound is required')
    stopped = datetime.fromisoformat(finished[0]['utc' if followup else 'utc_timestamp'].replace('Z','+00:00'))
    if stopped.tzinfo is None or stopped > datetime.now(timezone.utc):
        raise ValueError('Proven cleanup timestamp is not a past UTC instant')
    if followup:
        validate_followup_lineage(original, receipt, expected)
    return receipt, {'failure_receipt': bound(receipt_path), 'terminal_handle': handle,
        'terminal_handle_source': failure['terminal_handle_source'], 'process_snapshot': process_snapshot,
        'cleanup_timeline': timeline, 'stopped_upper_bound_utc': stopped.isoformat(),
        'deadline_credit_seconds': 0, 'execution_cost_reset': False}


def validate_followup_lineage(original, receipt, expected):
    lineage = receipt['lineage']
    for key in ('original_preparation','execution_result','native_cleanup','original_directory_inventory'):
        if bound(lineage[key]['path']) != lineage[key]:
            raise ValueError('Follow-up lineage artifact binding changed')
    initial = read_json(lineage['original_preparation']['path'])
    result = read_json(lineage['execution_result']['path'])
    cleanup = read_json(lineage['native_cleanup']['path'])
    if (initial.get('format') != FORMAT or plain_path(initial['runtime_root']) != original
            or result.get('status') != 'failed' or result.get('error',{}).get('type') != 'AssetUnavailable'
            or result.get('agent_reconciled') is not False or result.get('new_native_batches') != 0
            or any(result.get(key) is not True for key in
                   ('original_context_tasks_unchanged_no_recomputation','original_primary_task_ids_preserved',
                    'frozen_source_unchanged','model_assets_unchanged','original_run_unchanged',
                    'original_byte_copy_unchanged','owned_service_exited','owned_job_drained_and_closed'))
            or cleanup.get('queue_alive') is not False
            or any(row.get('worker_exited') is not True or row.get('job_closed') is not True
                   or row.get('job_drained') is not True for row in cleanup.get('workers',[]))):
        raise ValueError('Follow-up requires the proven stopped original recovery failure')
    if (result.get('actual_primary_task_count') != len(receipt['native']['primary_tasks'])
            or result.get('native_batch_id') != initial['batch_id']
            or result.get('scope_inputs') != initial['input_count']):
        raise ValueError('Follow-up changed the failed continuation scope')
    base = initial['preserved_primary_tasks']
    current = [row['task_id'] for row in receipt['native']['primary_tasks']]
    if (len(set(current)) != len(current) or not set(base) <= set(current)
            or lineage['base_primary_task_ids'] != base
            or set(lineage['added_primary_task_ids']) != set(current)-set(base)):
        raise ValueError('Follow-up task lineage must preserve every existing task')
    if read_json(lineage['original_directory_inventory']['path']) != directory_inventory(original):
        raise ValueError('Failed runtime directory scene changed')
    for key, root in (('original_inventory',initial['original_run']),
                      ('original_inventory',initial['original_byte_copy'])):
        binding = initial[key]
        if bound(binding['path']) != binding or file_inventory(root) != read_json(binding['path']):
            raise ValueError('Original H scene or byte evidence changed after failed recovery')
    for entry in receipt.get('restore_empty_directories',[]):
        if entry['relative'] != 'blobs':
            raise ValueError('Only the proven omitted original blob directory may be restored')
        root = plain_path(initial['original_run'])
        if (plain_path(entry['source']) != root/'blobs' or not (root/'blobs').is_dir()
                or any((root/'blobs').iterdir()) or (original/'blobs').exists()):
            raise ValueError('Omitted original empty blob directory proof is invalid')


def native_state(path):
    connection = connect(path, readonly=True)
    try:
        names = ('screen_batches','screen_items','screen_contexts','screen_tasks','tasks',
                 'ocr_submissions','versions','images','results','selections','screen_final_tasks','reviews')
        return {name: [dict(row) for row in connection.execute('SELECT * FROM '+quote(name)+' ORDER BY rowid')]
                for name in names}
    finally:
        connection.close()


def validate_native(state, receipt):
    if len(state['screen_batches']) != 1:
        raise ValueError('Recovery requires the one recorded native batch')
    batch = state['screen_batches'][0]
    expected = receipt['native']
    if any(batch[key] != expected['batch'][key] for key in ('id','project_id','request_id','payload_hash','deadline')):
        raise ValueError('Original batch identity or wall deadline changed')
    tasks = {row['id']: row for row in state['tasks']}
    contexts = state['screen_contexts']
    primary = state['screen_tasks']
    if (len(state['screen_items']) != receipt['inputs']['count'] or len(contexts) != expected['context_success_count']
            or {row['task_id'] for row in contexts} != {row['task_id'] for row in expected['context_tasks']}
            or {row['task_id'] for row in primary} != {row['task_id'] for row in expected['primary_tasks']}):
        raise ValueError('Original input, context or primary task scope changed')
    if any(tasks[row['task_id']]['status']!='succeeded' for row in contexts):
        raise ValueError('Only independently proven completed contexts may be reused')
    if any(state[name] for name in ('results','selections','screen_final_tasks','reviews')):
        raise ValueError('This continuation contract requires the original pre-result scene')
    followup = receipt.get('format') == FOLLOWUP_FORMAT
    allowed = {'queued','interrupted'} if followup else {'queued','running'}
    if not followup and len([row for row in tasks.values() if row['status']=='running']) != 1:
        raise ValueError('A unique abandoned active segment is required')
    if followup:
        if any(row['status']=='running' for row in tasks.values()):
            raise ValueError('Follow-up requires closed execution segments before copying')
        closed = [row for row in tasks.values() if row['id'] in {item['task_id'] for item in primary}
                  and row['status']=='interrupted']
        if (not closed or any(row['started'] is None or row['finished'] is None for row in closed)
                or fingerprint(closed) != receipt['lineage']['closed_execution_segments_sha256']):
            raise ValueError('Follow-up cannot reset or reclose historical execution costs')
        initial = read_json(receipt['lineage']['original_preparation']['path'])
        if ({row['task_id'] for row in contexts} != set(initial['successful_context_tasks'])
                or batch['id'] != initial['batch_id'] or batch['deadline'] != initial['deadline']):
            raise ValueError('Follow-up changed original contexts, batch or deadline')
    if any(tasks[row['task_id']]['engine']!='hunyuan' or tasks[row['task_id']]['status'] not in allowed for row in primary):
        raise ValueError('Existing H task identities or states changed')


def pixel_geometry_checks(state, runtime, original_inventory, original):
    checks = []
    versions = {row['id']: row for row in state['versions']}
    for item in state['screen_items']:
        version = versions[item['input_version_id']]
        path = plain_path(runtime/'native/ocr'/version['path'])
        if not path.is_relative_to(runtime) or file_sha(path) != item['source_hash'] or version['sha256'] != item['source_hash']:
            raise ValueError('Copied original input pixel/version binding changed')
        dimensions = None
        with path.open('rb') as stream:
            prefix = stream.read(24)
        if prefix[:8] == b'\x89PNG\r\n\x1a\n' and prefix[12:16] == b'IHDR':
            dimensions = list(struct.unpack('>II',prefix[16:24]))
        else:
            from PIL import Image
            with Image.open(path) as image:
                dimensions = list(image.size)
        if dimensions != [version['width'],version['height']]:
            raise ValueError('Copied source dimensions disagree with original geometry')
        checks.append({'kind':'original','input_version_id':item['input_version_id'],
                       'sha256':file_sha(path),'dimensions':dimensions,'matches':True})
        preparation = json.loads(item['preparation']) if item['preparation'] else {}
        for image in preparation.get('prepared_images',[]):
            target = plain_path(image['path'])
            if not target.is_relative_to(runtime):
                raise ValueError('Prepared image still reads outside the new runtime scope')
            source_member = target.relative_to(runtime).as_posix()
            expected = original_inventory.get(source_member)
            if not expected or file_sha(target) != expected['sha256']:
                raise ValueError('Copied prepared image changed')
            checks.append({'kind':'prepared','input_version_id':item['input_version_id'],
                           'prepared_id':image['id'],'sha256':file_sha(target),
                           'coordinate_sha256':fingerprint(coordinates(image)),'matches':True})
    for task in state['screen_tasks']:
        metadata = json.loads(task['metadata'])
        target = plain_path(runtime/'native/ocr'/task['prepared_path'])
        if (plain_path(metadata['path']) != target or not target.is_relative_to(runtime)
                or file_sha(target) != metadata['prepared_pixel_sha256']):
            raise ValueError('Frozen task preparation/pixel binding changed')
        checks.append({'kind':'task_prepared','task_id':task['task_id'],
            'sha256':file_sha(target),'coordinate_sha256':fingerprint(coordinates(metadata)),'matches':True})
    return checks


def scan_runtime_paths(runtime, original):
    found = []
    for path in sorted(runtime.rglob('*')):
        if not path.is_file():
            continue
        if path.suffix.lower() in {'.sqlite3','.sqlite','.db'}:
            connection = connect(path, readonly=True)
            try:
                for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                    for row in connection.execute('SELECT * FROM '+quote(table)):
                        for column, value in zip(row.keys(),row):
                            if isinstance(value,str) and is_original_reference(value,original):
                                found.append({'file':path.relative_to(runtime).as_posix(),'table':table,'column':column})
                            elif isinstance(value,bytes) and any(prefix in value.lower() for prefix in
                                    (str(original).encode().lower(),str(original).replace('\\','/').encode().lower())):
                                found.append({'file':path.relative_to(runtime).as_posix(),'table':table,'column':column,'opaque_binary':True})
            finally:
                connection.close()
        elif path.suffix.lower()=='.json' and 'credentials' not in path.parts:
            try:
                value = path.read_text('utf-8')
            except UnicodeError:
                continue
            if is_original_reference(value,original):
                found.append({'file':path.relative_to(runtime).as_posix()})
    if found:
        raise ValueError('Copied business records still reference the old scope: '+json.dumps(found))
    return {'old_scope_business_references':0,'opaque_checkpoint_references':0,
            'historical_logs_preserved':True,'credential_payloads_inspected':False}


def frozen_sources(path):
    path = plain_path(path)
    freeze = read_json(path)
    answer = {}
    for name, required in REQUIRED_SOURCE_FILES.items():
        member = freeze.get('sources',{}).get(name)
        root = plain_path(path.parent/name)
        if (not member or member.get('current_working_contents_frozen') is not True
                or not root.is_dir() or (root/'.git').exists()):
            raise ValueError('Complete source snapshot is required; mutable worktrees are refused')
        expected = member.get('files')
        if not isinstance(expected,dict) or not required <= set(expected):
            raise ValueError('Source manifest is incomplete for the native/Agent execution chain')
        actual = {key:value['sha256'] for key,value in file_inventory(root).items()}
        if actual != expected:
            raise ValueError('Frozen source files differ from the complete manifest')
        answer[name] = {'path':str(root),'files':expected,'inventory_sha256':fingerprint(expected)}
    return {'freeze':bound(path),'sources':answer,'complete_source_manifest_verified':True}


def prepare(original, output, failure_receipt, *, source_freeze=None):
    require_environment()
    original, output = plain_path(original), plain_path(output)
    if output.exists() or output.is_relative_to(original) or original.is_relative_to(output):
        raise ValueError('Recovery requires a new, nonoverlapping F-drive scope')
    expected = file_inventory(original)
    original_directories = directory_inventory(original)
    receipt, terminated = stop_evidence(original, failure_receipt, expected)
    sources = frozen_sources(source_freeze) if source_freeze is not None else None
    needed = 2*sum(value['bytes'] for value in expected.values())+sum(value['bytes'] for name,value in expected.items() if name.endswith('.sqlite3'))
    output.parent.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(output.parent).free < needed+32*1024*1024:
        raise ValueError('Insufficient F-drive space for preserved bytes and the runtime copy')
    output.mkdir(exist_ok=False)
    evidence, runtime = output/'evidence', output/'runtime'
    evidence.mkdir()
    copy_bytes(original, evidence/'original-byte-copy', expected, original_directories)
    copy_bytes(evidence/'original-byte-copy', runtime, expected, original_directories)
    frozen_json(evidence/'original-inventory.json',expected)
    frozen_json(evidence/'original-directories.json',original_directories)
    restored_directories = []
    for entry in receipt.get('restore_empty_directories',[]):
        destination = plain_path(runtime/entry['relative'])
        if destination.exists():
            raise ValueError('Directory restoration cannot overwrite copied state')
        destination.mkdir()
        restored_directories.append({'relative':entry['relative'],'source':entry['source'],
                                     'source_empty':True,'assets_created':0})
    databases = [runtime/name for name in expected if Path(name).suffix in {'.sqlite3','.sqlite','.db'}]
    sqlite_bindings = [consolidate_database(path,runtime) for path in databases]
    native_before = native_state(runtime/NATIVE_DB)
    validate_native(native_before,receipt)
    database_changes = []
    for path in databases:
        database_changes.extend(migrate_database_paths(path,original,runtime))
    json_changes = []
    for path in sorted(runtime.rglob('*.json')):
        if 'credentials' in path.parts:
            continue
        raw = path.read_text('utf-8')
        changed = rewrite_text(raw,original,runtime)
        if changed != raw:
            previous = bound(path)
            path.write_text(changed+'\n','utf-8')
            json_changes.append({'before':previous,'after':bound(path),
                'coordinate_sha256':fingerprint(coordinates(json.loads(raw))),
                'geometry_unchanged':coordinates(json.loads(raw))==coordinates(json.loads(changed))})
    stopped = datetime.fromisoformat(terminated['stopped_upper_bound_utc'])
    connection = connect(runtime/NATIVE_DB)
    state_changes = []
    try:
        for task in native_before['tasks']:
            if task['status']=='running':
                if task['finished'] is not None or task['started'] is None:
                    raise ValueError('Abandoned task lacks a unique open execution segment')
                started = datetime.fromisoformat(task['started'].replace('Z','+00:00'))
                if started.tzinfo is None or started > stopped:
                    raise ValueError('Cleanup bound precedes original task start')
                connection.execute("UPDATE tasks SET status='interrupted',phase='等待显式恢复',finished=? WHERE id=? AND status='running'", (stopped.isoformat(),task['id']))
                state_changes.append({'table':'tasks','id':task['id'],'from':'running','to':'interrupted',
                    'started_unchanged':task['started'],'finished_upper_bound':stopped.isoformat(),
                    'old_segment_upper_bound_seconds':(stopped-started).total_seconds(),'carried_spent_unchanged':True})
            elif task['status']=='queued':
                connection.execute("UPDATE tasks SET status='paused',phase='等待显式恢复' WHERE id=? AND status='queued'",(task['id'],))
                state_changes.append({'table':'tasks','id':task['id'],'from':'queued','to':'paused'})
        connection.execute("UPDATE screen_items SET status='paused' WHERE batch_id=? AND status NOT IN ('completed','needs_review','failed','cancelled')",(native_before['screen_batches'][0]['id'],))
        connection.execute("UPDATE screen_batches SET status='paused' WHERE id=?",(native_before['screen_batches'][0]['id'],))
        connection.commit()
    finally:
        connection.close()
    native_after = native_state(runtime/NATIVE_DB)
    if ({row['id'] for row in native_before['tasks']} != {row['id'] for row in native_after['tasks']}
            or len(native_before['screen_tasks'])!=len(native_after['screen_tasks'])
            or native_before['ocr_submissions']!=native_after['ocr_submissions']):
        raise ValueError('Recovery created or replaced an original task/submission identity')
    for before, after in zip(native_before['tasks'],native_after['tasks']):
        for key in before:
            if key not in {'status','phase','finished'} and before[key]!=after[key]:
                raise ValueError('Recovery changed an original task identity, attempt or start')
        if before['status']!='running' and before['finished']!=after['finished']:
            raise ValueError('Recovery changed a closed historical execution segment')
    for before, after in zip(native_before['screen_tasks'],native_after['screen_tasks']):
        if before['spent_seconds']!=after['spent_seconds'] or before['attempt']!=after['attempt']:
            raise ValueError('Recovery reset carried execution cost or attempt')
        old, new = json.loads(before['metadata']), json.loads(after['metadata'])
        if remap_value(old,original,runtime)!=new:
            raise ValueError('Recovery changed frozen model identity, geometry or budget')
    if native_before['screen_contexts'] != native_after['screen_contexts']:
        raise ValueError('Recovery changed successful context bytes or carried costs')
    before_batch, after_batch = native_before['screen_batches'][0],native_after['screen_batches'][0]
    for key in before_batch:
        if key!='status' and before_batch[key]!=after_batch[key]:
            raise ValueError('Recovery changed batch policy, identity, deadline or paused allowance')
    checks = pixel_geometry_checks(native_after,runtime,expected,original)
    scan = scan_runtime_paths(runtime,original)
    if (file_inventory(evidence/'original-byte-copy')!=expected or file_inventory(original)!=expected
            or directory_inventory(original)!=original_directories
            or directory_inventory(evidence/'original-byte-copy')!=original_directories):
        raise ValueError('Original scene or preserved byte evidence changed')
    frozen_json(evidence/'native-state-before.json',native_before)
    frozen_json(evidence/'native-state-after.json',native_after)
    frozen_json(evidence/'database-path-migrations.json',database_changes)
    frozen_json(evidence/'json-path-migrations.json',json_changes)
    frozen_json(evidence/'task-state-deltas.json',state_changes)
    frozen_json(evidence/'pixel-and-geometry-readback.json',checks)
    runtime_inventory = file_inventory(runtime)
    frozen_json(evidence/'runtime-inventory.json',runtime_inventory)
    frozen_json(evidence/'runtime-directories.json',directory_inventory(runtime))
    result = {'format':FORMAT,'stage':'prepared_source_bound' if sources else 'awaiting_frozen_source_binding',
        'original_run':str(original),'recovery_root':str(output),'runtime_root':str(runtime),
        'original_inventory':bound(evidence/'original-inventory.json'),
        'runtime_inventory':bound(evidence/'runtime-inventory.json'),
        'original_directories':bound(evidence/'original-directories.json'),
        'runtime_directories':bound(evidence/'runtime-directories.json'),
        'restored_empty_directories':restored_directories,
        'original_byte_copy':str(evidence/'original-byte-copy'),
        'all_original_files_unchanged':True,'original_sqlite_opened':False,
        'sqlite_byte_copy_evidence_opened':False,'sqlite_logical_copies':sqlite_bindings,
        'termination_evidence':terminated,'source_binding':sources,
        'batch_id':before_batch['id'],'native_project_id':before_batch['project_id'],
        'request_id':before_batch['request_id'],'deadline':before_batch['deadline'],
        'deadline_increased':False,'execution_costs_reset':False,
        'input_count':len(native_after['screen_items']),
        'successful_context_tasks':[row['task_id'] for row in native_after['screen_contexts']],
        'preserved_primary_tasks':[row['task_id'] for row in native_after['screen_tasks']],
        'primary_tasks_created':0,'scope_paused_before_service_start':True,
        'task_state_deltas':bound(evidence/'task-state-deltas.json'),
        'database_path_migrations':bound(evidence/'database-path-migrations.json'),
        'json_path_migrations':bound(evidence/'json-path-migrations.json'),
        'pixel_geometry_readback':bound(evidence/'pixel-and-geometry-readback.json'),
        'path_scan':scan,'truth_read':False,'service_started':False,'model_invoked':False,
        'agent_reconciled':False,'controller_values_emitted':False,
        'launch_requires_validated_source_binding':True,'performance_baseline_qualified':False}
    if receipt.get('format') == FOLLOWUP_FORMAT:
        result['lineage'] = receipt['lineage']
        result['continuation_kind'] = 'failed-recovery-followup'
    frozen_json(output/'prepared-recovery.json',result)
    return result


def bind_source(prepared_path, freeze_path, output_path):
    prepared_path, output_path = plain_path(prepared_path), plain_path(output_path)
    prepared = read_json(prepared_path)
    if prepared.get('format')!=FORMAT or prepared.get('service_started') is not False:
        raise ValueError('Recovery preparation contract is invalid')
    runtime = plain_path(prepared['runtime_root'])
    if file_inventory(runtime)!=read_json(prepared['runtime_inventory']['path']):
        raise ValueError('Prepared runtime changed before source binding')
    if prepared.get('runtime_directories') and directory_inventory(runtime)!=read_json(prepared['runtime_directories']['path']):
        raise ValueError('Prepared runtime directory structure changed before source binding')
    if output_path.exists() or not output_path.is_relative_to(plain_path(prepared['recovery_root'])):
        raise ValueError('Source binding must be a new receipt inside the recovery scope')
    sources = frozen_sources(freeze_path)
    result = {'format':'controlled-native-recovery-source-binding-v1','prepared':bound(prepared_path),
        'sources':sources,'runtime_root':str(runtime),'native_batch_id':prepared['batch_id'],
        'deadline':prepared['deadline'],'model_or_service_started':False,
        'runtime_unchanged_before_binding':True,'performance_baseline_qualified':False}
    frozen_json(output_path,result)
    return result


def validate_launch(prepared_path, source_binding_path=None):
    prepared_path = plain_path(prepared_path)
    prepared = read_json(prepared_path)
    if prepared.get('format')!=FORMAT or prepared.get('service_started') is not False:
        raise ValueError('Invalid recovery preparation')
    if source_binding_path is None:
        sources = prepared.get('source_binding')
        if sources is None:
            raise ValueError('Recovery launch refused until complete frozen sources are bound')
    else:
        binding = read_json(plain_path(source_binding_path))
        if (binding.get('format')!='controlled-native-recovery-source-binding-v1'
                or binding['prepared']!=bound(prepared_path) or binding['runtime_root']!=prepared['runtime_root']):
            raise ValueError('Source binding is for a different preparation scope')
        sources = binding['sources']
    if frozen_sources(sources['freeze']['path'])!=sources:
        raise ValueError('Frozen source binding changed before launch')
    runtime = plain_path(prepared['runtime_root'])
    inventory_path = plain_path(prepared['runtime_inventory']['path'])
    if (bound(inventory_path)!=prepared['runtime_inventory']
            or file_inventory(runtime)!=read_json(inventory_path)):
        raise ValueError('Prepared runtime changed before explicit launch')
    if prepared.get('runtime_directories'):
        binding = prepared['runtime_directories']
        if bound(binding['path'])!=binding or directory_inventory(runtime)!=read_json(binding['path']):
            raise ValueError('Prepared runtime directory structure changed before explicit launch')
    if prepared['deadline'] is not None and prepared['deadline']<=datetime.now(timezone.utc).timestamp():
        raise ValueError('Original batch wall allowance expired; it cannot be reset')
    scan_runtime_paths(runtime,plain_path(prepared['original_run']))
    return {'launch_source_and_scope_verified':True,'runtime_root':str(runtime),
            'sources':sources,'batch_id':prepared['batch_id'],'explicit_original_batch_resume_required':True,
            'service_or_model_started_by_this_helper':False,'performance_baseline_qualified':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    item = sub.add_parser('prepare')
    item.add_argument('--original-run',required=True)
    item.add_argument('--output',required=True)
    item.add_argument('--failure-receipt',required=True)
    item.add_argument('--source-freeze')
    item = sub.add_parser('bind-source')
    item.add_argument('--prepared',required=True)
    item.add_argument('--source-freeze',required=True)
    item.add_argument('--output',required=True)
    item = sub.add_parser('verify-launch')
    item.add_argument('--prepared',required=True)
    item.add_argument('--source-binding')
    args = parser.parse_args()
    if args.command=='prepare':
        value = prepare(args.original_run,args.output,args.failure_receipt,source_freeze=args.source_freeze)
        print(json.dumps({key:value[key] for key in ('stage','runtime_root','batch_id','input_count','primary_tasks_created','service_started','model_invoked')},ensure_ascii=False))
    elif args.command=='bind-source':
        value = bind_source(args.prepared,args.source_freeze,args.output)
        print(json.dumps({'source_bound':True,'runtime_root':value['runtime_root'],'model_or_service_started':False}))
    else:
        value = validate_launch(args.prepared,args.source_binding)
        print(json.dumps({key:value[key] for key in ('launch_source_and_scope_verified','runtime_root','batch_id','explicit_original_batch_resume_required','service_or_model_started_by_this_helper')},ensure_ascii=False))


if __name__=='__main__':
    main()
