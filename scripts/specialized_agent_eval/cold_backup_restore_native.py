"""Actual 12-image native/Agent/data cold restore on new F-only copies.

Original inference and S07 roots are byte-inventoried, never opened by SQLite.
Only owned copied/restored roots receive WAL recovery, path relocation or writes.
Native readers disable every queue and Agent. No model/controller is started.
"""
from __future__ import annotations
import argparse
import asyncio
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import digest, fingerprint, fpath, frozen_json, read_json


def require(value, message):
    if not value:
        raise ValueError(message)


def inventory(root):
    root = fpath(root)
    result = {}
    for path in sorted(root.rglob('*')):
        require(not path.is_symlink() and not path.is_junction(), 'Linked evidence source is forbidden')
        if path.is_file():
            require(fpath(path).is_relative_to(root), 'Evidence file escapes its F root')
            result[path.relative_to(root).as_posix()] = digest(path)
    return result


def freeze_consumers(args, output):
    """Run exact frozen current consumer bytes despite concurrent branch work."""
    files, roots = {}, {}
    ignored = {'__pycache__', '.pytest_cache', '.git', 'node_modules'}
    for name, value, members in (
            ('platform', args.platform_source, ('src', 'scripts', 'packages', 'modules', 'config', 'pyproject.toml')),
            ('ocr', args.native_source, ('src', 'config'))):
        source = fpath(value); target = output / 'consumers' / name
        records = {}
        for member in members:
            start = source / member
            for path in ([start] if start.is_file() else sorted(start.rglob('*'))):
                require(not path.is_symlink() and not path.is_junction(), 'Linked consumer source is forbidden')
                if not path.is_file() or ignored.intersection(path.parts) or path.suffix == '.pyc':
                    continue
                relative = path.relative_to(source).as_posix()
                expected = digest(path); copied = target / relative
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, copied)
                require(digest(copied) == expected == digest(path), 'Consumer source changed while freezing it')
                records[relative] = expected
        require(records, 'Actual consumer source inventory is empty')
        files.update({name + '/' + key: value for key, value in records.items()})
        roots[name] = {'upstream': str(source), 'frozen': str(target), 'files': len(records)}
    bindings = {'format': 'clear-cold-consumer-source-v1', 'roots': roots, 'files_sha256': files,
                'inventory_sha256': fingerprint(files), 'method': 'Exact current source bytes frozen before imports'}
    frozen_json(output / 'consumer-source-inventory.json', bindings)
    args.platform_source = roots['platform']['frozen']; args.native_source = roots['ocr']['frozen']
    return bindings


def install_native_read_audit(log_path, data_root, forbidden_roots):
    """Log real open events; forbid all old data scopes without altering them."""
    data_root = fpath(data_root)
    denied = tuple(fpath(root) for root in forbidden_roots)
    descriptor = os.open(fpath(log_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    def audit(event, arguments):
        if event != 'open' or not isinstance(arguments[0], (str, bytes)):
            return
        value = os.fsdecode(arguments[0]); path = Path(value).resolve()
        if path.drive.upper() != 'F:':
            return
        old_scope = next((root for root in denied if path.is_relative_to(root)), None)
        if not old_scope and not path.is_relative_to(data_root):
            return
        frames = []; frame = sys._getframe(1)
        for _ in range(10):
            if frame is None: break
            frames.append({'file': frame.f_code.co_filename, 'function': frame.f_code.co_name, 'line': frame.f_lineno})
            frame = frame.f_back
        record = {'path': str(path), 'mode': arguments[1], 'flags': arguments[2],
                  'forbidden_old_scope': str(old_scope) if old_scope else None, 'frames': frames}
        os.write(descriptor, (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'))
        if old_scope:
            raise PermissionError('Cold restored consumer attempted an old data scope')
    sys.addaudithook(audit)


def native_bytes(base, token, path):
    parsed = urlsplit(base)
    require(parsed.hostname == '127.0.0.1' and parsed.port, 'Native binary reader requires owned loopback')
    client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
    try:
        client.request('GET', path, headers={'Authorization': 'Bearer ' + token})
        response = client.getresponse(); payload = response.read(32 * 1024 * 1024 + 1)
        require(response.status == 200 and len(payload) <= 32 * 1024 * 1024, 'Actual binary native GET failed')
        return payload
    finally:
        client.close()


def copy_actual(source, destination, mappings):
    """Copy DB together with WAL/SHM as opaque bytes; open only the destination."""
    source, destination = fpath(source), fpath(destination)
    require(not source.is_relative_to(destination) and not destination.is_relative_to(source),
            'Cold copy destination must be outside its original source')
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for old_name, new_name in mappings.items():
        old, new = source / old_name, destination / new_name
        if not old.exists():
            continue
        require(not new.exists(), 'Never overwrite an existing copied member')
        if old.is_dir():
            shutil.copytree(old, new)
            require(inventory(old) == inventory(new), 'Full copied directory/WAL bytes differ')
            records.append({'source': str(old), 'target': str(new), 'files_sha256': inventory(old)})
        else:
            for member in (old, old.with_name(old.name + '-wal'), old.with_name(old.name + '-shm')):
                if member.exists():
                    target = new.with_name(new.name + member.name[len(old.name):])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(member, target)
                    require(digest(member) == digest(target), 'Exact DB/WAL/SHM copy differs')
                    records.append({'source': str(member), 'target': str(target), 'sha256': digest(member)})
    return records


def checkpoint_owned(root):
    receipts = []
    for path in sorted(root.rglob('*.sqlite3')):
        with closing(sqlite3.connect(path)) as db:
            require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'Copied SQLite integrity failed')
            reply = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            require(reply[0] == 0, 'Owned copied WAL checkpoint remained busy')
        receipts.append({'path': str(path), 'quick_check': 'ok', 'checkpoint': list(reply)})
    return receipts


def database_state(root):
    """Normalize only relocated live root paths; preserve all business row values."""
    from scripts.product_compatibility import read_database
    root = fpath(root)
    result = {}
    def normalized(value, table, column):
        if isinstance(value, bytes):
            return {'binary_sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}
        if isinstance(value, str):
            if table == 'derived_inputs' and column == 'provenance_path':
                return 'bridges/' + Path(value).name
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return normalized(parsed, table, column)
            path = Path(value)
            if path.is_absolute() and path.is_relative_to(root):
                return '<DATA_ROOT>/' + path.relative_to(root).as_posix()
        if isinstance(value, dict):
            return {key: normalized(item, table, key) for key, item in value.items()}
        if isinstance(value, list):
            return [normalized(item, table, column) for item in value]
        return value
    for path in sorted(root.rglob('*.sqlite3')):
        tables = {}
        with closing(read_database(path)) as db:
            for name, schema in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
                quoted = '"' + name.replace('"', '""') + '"'
                columns = [row[1] for row in db.execute('PRAGMA table_info(' + quoted + ')')]
                rows = [[normalized(value, name, column) for column, value in zip(columns, row)]
                        for row in db.execute('SELECT * FROM ' + quoted)]
                serialized = sorted(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(',', ':')) for row in rows)
                tables[name] = {'rows': len(rows), 'schema_sha256': fingerprint(schema), 'rows_sha256': fingerprint(serialized)}
            version = db.execute('PRAGMA user_version').fetchone()[0]
        result[path.relative_to(root).as_posix()] = {'user_version': version, 'tables': tables}
    return result


def platform_services(root, data_python):
    from fastapi import FastAPI
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetStore
    from workbench_platform.store import WorkspaceStore
    from workbench_platform.data_api.service import DataWorkbenchService
    from workbench_platform.artifacts.center import ArtifactCenter
    from workbench_platform.audit.service import AuditService
    from workbench_platform.batches.service import BatchService, ACTIVE
    from workbench_platform.batches.store import BatchStore
    from workbench_platform.agent.store import AgentStore
    from workbench_platform.agent.preferences import TaskPreferences
    from workbench_platform.agent.skills_routes import router
    uploads = root / 'asset-uploads'
    for path in (uploads, uploads / 'replay', uploads / 'reference', root / 'asset-blobs'):
        path.mkdir(parents=True, exist_ok=True)
    workspaces = WorkspaceStore(root / 'workspaces.sqlite3')
    assets = AssetRegistry(root / 'assets.sqlite3', root / 'asset-blobs', {
        'replay': uploads / 'replay', 'uploads': uploads, 's07reference': uploads / 'reference'})
    sets = AssetSetStore(assets)
    data = DataWorkbenchService(root / 'data-workbench', data_python); data.asset_registry = assets
    center = ArtifactCenter(root / 'artifact-center', assets, sets, data)
    store = AgentStore(root / 'agent')
    agent = SimpleNamespace(store=store, preferences=TaskPreferences(store))
    app = FastAPI()
    app.state.workspace_store = workspaces; app.state.asset_library = (assets, sets, None)
    app.state.data_workbench = data; app.state.artifact_center = center; app.state.agent = agent
    batches = BatchStore(root / 'batches')
    require(not any(row['status'] in ACTIVE for row in batches.list()),
            'Read-only cold consumer refuses to resume an active processing batch')
    app.state.batch_service = BatchService(app, root / 'batches')
    app.state.batch_store = app.state.batch_service.store
    app.state.audit_service = AuditService(app, root / 'audit')
    agent.services = app.state
    app.include_router(router)
    return app, data


async def initialize_saver(root):
    from workbench_platform.agent.dependencies import load_dependencies
    _, Saver = load_dependencies()
    target = root / 'agent/checkpoints.sqlite3'
    if target.exists():
        return {'initialized': False, 'reason': 'Existing actual checkpoint store preserved'}
    async with Saver.from_conn_string(str(target)) as saver:
        await saver.setup()
    return {'initialized': True, 'path': str(target), 'method': 'Existing AsyncSqliteSaver.setup only',
            'checkpoints_created': 0, 'graph_started': False, 'controller_calls': 0,
            'scope': 'S07 deterministic tool runner had no graph history; no historical checkpoint is invented'}


def preferences_api(root, data_python, *, save_if_absent=False):
    from fastapi.testclient import TestClient
    app, data = platform_services(root, data_python)
    endpoint = '/api/platform/v1/workspaces/wreplay/agent/preferences'
    receipts = []
    try:
        with TestClient(app, base_url='http://127.0.0.1:8891', headers={'Origin': 'http://127.0.0.1:8891'}) as client:
            before = client.get(endpoint, params={'business': 'screen_excel'})
            require(before.status_code == 200, 'Actual preference read API failed')
            store = app.state.agent.store
            frozen_before = store.rows('SELECT * FROM agent_preference_snapshots ORDER BY run_id,business')
            if save_if_absent and before.json()['effective_values'].get('output_layout') != 'per_image':
                body = {'request_id': 'coldRestoreAuthorizedPerImage', 'business': 'screen_excel',
                    'expected_revision': before.json()['profile']['revision'], 'values': {'output_layout': 'per_image'},
                    'revoke_keys': [], 'source': {'mode': 'user_form',
                        'quote': '用户明确：每张图片单独一个 Excel 文件；本轮已授权在独立冷副本保存此输出偏好，同图多表分 sheet、整批 ZIP。'}}
                written = client.post(endpoint, json=body)
                require(written.status_code == 200, 'Actual production preference save API failed')
                receipts.append({'method': 'POST', 'endpoint': endpoint, 'request': body, 'actual_response': written.json(),
                    'scope': 'Explicit root/user-authorized copied workspace only; original run preferences not claimed or changed'})
            require(store.rows('SELECT * FROM agent_preference_snapshots ORDER BY run_id,business') == frozen_before,
                    'Saving new defaults changed an existing frozen run snapshot')
            after = client.get(endpoint, params={'business': 'screen_excel'})
            require(after.status_code == 200, 'Actual saved preference read API failed')
            receipts.append({'method': 'GET', 'endpoint': endpoint, 'actual_response': after.json()})
            return {'preview': after.json(), 'frozen_snapshots': frozen_before, 'api_receipts': receipts,
                    'old_frozen_snapshots_unchanged': True}
    finally:
        app.state.batch_service.close()
        data.close()


def serve(args):
    install_native_read_audit(args.read_audit_log, args.native_data, args.forbid_data_root)
    sys.path[:0] = [str(fpath(args.native_source) / 'src'), str(fpath(args.pdf_layer).parent)]
    from ocr_workbench.service import create_app
    import uvicorn
    app = create_app(fpath(args.bundle), fpath(args.native_data), fpath(args.token_file).read_text('utf-8'),
                     start_queue=False, agent_enabled=False)
    from ocr_workbench.screen_diagnostics import VERSION
    loaded = {name: {'path': str(fpath(module.__file__)), 'sha256': digest(module.__file__)}
              for name, module in sorted(sys.modules.items())
              if name.startswith('ocr_workbench') and getattr(module, '__file__', None)}
    require(all(fpath(row['path']).is_relative_to(fpath(args.native_source)) for row in loaded.values()),
            'Native reader loaded code outside its tested frozen consumer scope')
    frozen_json(args.reader_runtime_report, {'executable': str(fpath(sys.executable)),
        'executable_sha256': digest(sys.executable), 'python_version': sys.version,
        'diagnostics_version': VERSION, 'loaded_native_modules': loaded,
        'native_source': str(fpath(args.native_source)), 'start_queue': False, 'agent_enabled': False,
        'forbidden_data_roots': args.forbid_data_root})
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


def read_native(args, data, output, phase):
    from s07_native_read import native_http
    token_file = output / (phase + '-reader-token.txt')
    token_file.write_text(secrets.token_urlsafe(36), encoding='utf-8')
    token = token_file.read_text('utf-8')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    base = 'http://127.0.0.1:' + str(port)
    command = [str(fpath(args.native_python)), '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--serve',
        '--native-source', str(fpath(args.native_source)), '--bundle', str(fpath(args.bundle)),
        '--native-data', str(data), '--token-file', str(token_file), '--pdf-layer', str(fpath(args.pdf_layer)), '--port', str(port),
        '--read-audit-log', str(output / (phase + '-native-file-reads.jsonl')),
        '--reader-runtime-report', str(output / (phase + '-native-runtime.json'))]
    forbidden = [fpath(args.source_run), fpath(args.s07_run)]
    if phase == 'restored':
        forbidden.extend([output / 'sources', output / 'isolated-cold-sources'])
    for root in forbidden:
        command.extend(['--forbid-data-root', str(root)])
    process = None; result = {}
    try:
        with (output / (phase + '-native-reader.log')).open('wb') as log:
            process = subprocess.Popen(command, cwd=output, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.perf_counter() + 60
            while time.perf_counter() < deadline:
                require(process.poll() is None, 'Owned no-queue reader exited; inspect F log')
                try:
                    state = native_http(base, token, '/api/state'); break
                except (ValueError, OSError):
                    time.sleep(.15)
            else:
                raise ValueError('Owned no-queue reader did not become ready')
            require(not any((state.get(name) or {}).get('alive') for name in ('queue', 'fusion_queue', 'external_queue')),
                    'Read-only native reader started a compute queue')
            with closing(sqlite3.connect((data / 'workbench.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                batches = [dict(row) for row in db.execute('SELECT id,project_id,policy,deadline FROM screen_batches')]
                items = [dict(row) for row in db.execute('SELECT * FROM screen_items ORDER BY position')]
                versions = {row['id']: dict(row) for row in db.execute('SELECT * FROM versions')}
            require(len(batches) == 1 and len(items) == 12, 'Actual native cold check requires one 12-input batch')
            batch = batches[0]; endpoint = '/api/projects/' + batch['project_id'] + '/screen-batches/' + batch['id']
            business_edit = None
            if phase == 'business-edit':
                business_edit = seed_business_edit(base, token, endpoint, items, output)
                with closing(sqlite3.connect((data / 'workbench.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                    db.row_factory=sqlite3.Row
                    items=[dict(row) for row in db.execute('SELECT * FROM screen_items ORDER BY position')]
            snapshot = native_http(base, token, endpoint + '/delivery-snapshot')
            require(snapshot['fresh'], 'Actual native snapshot is stale before/after restore')
            files, details, read_clocks = {}, [], []
            for item in items:
                version = versions[item['input_version_id']]
                original = data / version['path']
                require(original.resolve().is_relative_to(data) and digest(original) == version['sha256'], 'Restored original file SHA differs')
                files[original.relative_to(data).as_posix()] = digest(original)
                clock_before = time.time()
                detail = native_http(base, token, endpoint + '/items/' + item['input_version_id'])
                clock_after = time.time()
                input_path = fpath(detail['preparation']['input_path'])
                require(input_path == original and digest(input_path) == detail['preparation']['input_sha256'],
                        'Actual diagnostics input path is not its owned restored original version')
                image_endpoint = detail['image_url']
                require(hashlib.sha256(native_bytes(base, token, image_endpoint)).hexdigest() == version['sha256'],
                        'Restored original HTTP download SHA differs')
                downloads = [{'endpoint': image_endpoint, 'path': str(input_path), 'sha256': version['sha256']}]
                for crop in detail['preparation'].get('prepared_images', []):
                    path = fpath(crop['path'])
                    require(path.is_relative_to(data), 'Restored live prepared path still points to an old root')
                    files[path.relative_to(data).as_posix()] = digest(path)
                    image_endpoint = endpoint + '/items/' + item['input_version_id'] + '/files/' + quote(crop['id'], safe='')
                    require(hashlib.sha256(native_bytes(base, token, image_endpoint)).hexdigest() == digest(path),
                            'Restored prepared HTTP download SHA differs')
                    downloads.append({'endpoint': image_endpoint, 'relative_path': path.relative_to(data).as_posix(), 'sha256': digest(path)})
                tables = native_http(base, token, '/api/results/' + item['result_id'] + '/revisions/' + str(item['result_revision']) + '/tables')
                diagnostics = detail.get('diagnostics') or {}
                require(diagnostics.get('version') == 'screen-diagnostics-v2', 'Actual consumer is not diagnostics v2')
                countdown = diagnostics['execution_budget']['wall_deadline_remaining_seconds']
                if batch['deadline']:
                    require(isinstance(countdown, (int, float))
                        and max(0., batch['deadline'] - clock_after) - .05 <= countdown
                        <= max(0., batch['deadline'] - clock_before) + .05,
                        'Actual diagnostic countdown does not match the persisted deadline and real request time')
                else:
                    require(countdown is None, 'Actual deadline-free diagnostic countdown must be None')
                read_clocks.append({'input_version_id': item['input_version_id'], 'persisted_deadline': batch['deadline'],
                    'request_started_unix': clock_before, 'request_finished_unix': clock_after,
                    'actual_wall_deadline_remaining_seconds': countdown, 'persisted_deadline_formula_verified': True})
                frozen_json(output / (phase + '-diagnostics-' + str(item['position']) + '.json'), diagnostics)
                compared_diagnostics = json.loads(json.dumps(diagnostics))
                compared_diagnostics['execution_budget']['wall_deadline_remaining_seconds'] = '<LIVE_DEADLINE_MINUS_REQUEST_TIME>'
                details.append({'input_version_id': item['input_version_id'], 'result_id': item['result_id'],
                    'revision': item['result_revision'], 'status': item['status'], 'review_state': item['review_state'],
                    'candidates_sha256': fingerprint(json.loads(item['candidates'])), 'actual_tables_sha256': fingerprint(tables),
                    'business_metadata_sha256': fingerprint(detail.get('business_metadata', [])),
                    'metadata_edit_log_sha256': fingerprint((detail.get('result', {}).get('edited', {}).get('screen') or {}).get('metadata_edit_log', [])),
                    'diagnostics': compared_diagnostics,
                    'downloaded_images': [{key: value for key, value in row.items() if key != 'path'} for row in downloads]})
            export_root = data / 'screen-batches' / batch['id'] / 'exports'
            manifests = sorted(export_root.glob('*/manifest.json'), key=lambda path: path.stat().st_mtime_ns)
            require(manifests, 'Actual source native batch has no existing export manifest')
            exported = read_json(manifests[-1])
            options = exported['export_options']
            vector = {**{key: snapshot[key] for key in
                ('snapshot_id', 'batch_id', 'project_id', 'policy_sha256', 'items')}, 'export_options': options}
            require(exported['version_vector'] == vector, 'Existing actual export is not the current full version vector')
            export_files = {path.relative_to(data).as_posix(): digest(path)
                            for path in export_root.rglob('*') if path.is_file()}
            result = {'snapshot': snapshot, 'version_vector': {**{key: snapshot[key] for key in
                ('snapshot_id', 'batch_id', 'project_id', 'policy_sha256', 'items')}, 'export_options': options},
                'policy': json.loads(batch['policy']), 'details': details, 'source_and_prepared_files': files,
                'existing_export_files': export_files,
                'actual_diagnostic_clock_verification': read_clocks,
                'queue_alive': False, 'fusion_queue_alive': False, 'external_queue_alive': False,
                'start_queue': False, 'agent_enabled': False, 'only_http_get': business_edit is None,
                'business_metadata_probe': business_edit, 'pid': process.pid}
            frozen_json(output / (phase + '-native-read.json'), result)
    finally:
        if process is not None and process.poll() is None:
            process.terminate(); process.wait(timeout=15)
        frozen_json(output / (phase + '-native-stop.json'), {'pid': process.pid if process else None,
            'owned_process_exited': process is None or process.poll() is not None,
            'model_calls': 0, 'controller_calls': 0})
    audit_path = output / (phase + '-native-file-reads.jsonl')
    actual_reads = [json.loads(line) for line in audit_path.read_text('utf-8').splitlines()]
    require(not any(row['forbidden_old_scope'] for row in actual_reads), 'Native consumer attempted to read an old data scope')
    pixel_reads = [row for row in actual_reads
                   if any(Path(frame['file']).name == 'screen_diagnostics.py' for frame in row['frames'])]
    require(pixel_reads, 'Diagnostics v2 did not actually read any owned source pixels')
    require(all(fpath(row['path']).is_relative_to(data) for row in pixel_reads), 'Diagnostics source pixel read escaped owned data')
    result['actual_read_audit'] = {'path': str(audit_path), 'sha256': digest(audit_path),
        'events': len(actual_reads), 'forbidden_old_scope_reads': 0, 'diagnostics_source_pixel_read_events': len(pixel_reads),
        'diagnostics_source_pixel_paths': sorted({str(fpath(row['path'])) for row in pixel_reads}),
        'runtime': read_json(output / (phase + '-native-runtime.json'))}
    frozen_json(output / (phase + '-native-read-with-audit.json'), result)
    return result


def seed_business_edit(base, token, endpoint, items, output):
    """Write an explicit unconfirmed engineering edit only in the owned copy."""
    from s07_native_read import native_http
    def post(path, body):
        parsed=urlsplit(base)
        require(parsed.hostname=='127.0.0.1' and parsed.port, 'Edit probe must use its owned loopback reader')
        connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=30)
        try:
            connection.request('POST',path,json.dumps(body,ensure_ascii=False).encode('utf8'),
                {'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            reply=connection.getresponse();payload=reply.read(32*1024*1024+1)
            require(reply.status==200 and len(payload)<=32*1024*1024,'Owned business edit/export was rejected')
            return payload
        finally:connection.close()
    before=native_http(base,token,endpoint+'/delivery-snapshot')
    for item in items:
        detail=native_http(base,token,endpoint+'/items/'+item['input_version_id'])
        metadata=detail.get('business_metadata',[])
        if metadata:break
    else:raise ValueError('Cold edit qualification needs an actual metadata-bearing result')
    result=detail['result'];original_sha=fingerprint(result['original'])
    text=metadata[0]['text']+' [冷备工程校验，未确认]'
    body={'action':'review','state':'retain','version_ids':[item['input_version_id']],
        'request_id':'coldBusinessMetadataProbe','result_id':result['id'],
        'expected_revision':result['revision'],'expected_review_token':detail['review_binding_token'],
        'metadata_edits':[{'item_index':0,'before':metadata[0]['text'],'after':text,'choice':'manual'}]}
    receipt=json.loads(post(endpoint+'/action',body))
    require(json.loads(post(endpoint+'/action',body))==receipt,'Repeated business edit changed its original receipt')
    current=native_http(base,token,endpoint+'/items/'+item['input_version_id'])
    require(current['result']['revision']==result['revision']+1 and
        fingerprint(current['result']['original'])==original_sha,'Engineering edit changed original recognition or wrong revision')
    require(current['business_metadata'][0]['text']==text and
        not current['business_metadata'][0].get('human_confirmed'),'Unconfirmed engineering edit lost its text or claimed signoff')
    after=native_http(base,token,endpoint+'/delivery-snapshot')
    require(before['snapshot_id']!=after['snapshot_id'],'Business edit did not invalidate the previous delivery snapshot')
    archive=post(endpoint+'/export',{'include_pending_review':True})
    path=output/'business-edit-current.zip';path.write_bytes(archive)
    proof={'input_version_id':item['input_version_id'],'result_id':result['id'],
        'before_revision':result['revision'],'after_revision':current['result']['revision'],
        'original_recognition_sha256':original_sha,'before_snapshot_id':before['snapshot_id'],
        'after_snapshot_id':after['snapshot_id'],'actual_action_receipt':receipt,
        'business_metadata_sha256':fingerprint(current['business_metadata']),
        'current_export':{'path':str(path),'sha256':digest(path)},
        'engineering_test_text_only':True,'human_signoff':False,'model_calls':0}
    frozen_json(output/'business-metadata-edit-probe.json',proof)
    return proof


async def read_data(root, data_python, source_report, output):
    from workbench_platform.agent.registry import ToolContext, ToolRegistry
    from workbench_platform.agent.tools import data as data_tools, audit
    from workbench_platform.application_services.references import verify as verify_ref
    app, service = platform_services(root, data_python)
    store = app.state.agent.store
    old_session = store.session(source_report['session_id'])
    session = store.create_session(old_session['workspace_id'], '真实冷恢复只读核验', old_session['selected_assets'], 'coldRestoreReadSession')
    run, _ = store.start_run(session['id'], '只读核验恢复后的真实数据和证据；不重新计算，不启动模型。', 'coldRestoreReadRun', 0)
    context = ToolContext(old_session['workspace_id'], session['id'], run['id'], run['generation'], 0, '',
        tuple(session['selected_assets']), app.state, store)
    registry = ToolRegistry(); data_tools.register(registry); audit.register(registry)
    source_ref = source_report['data_result']['result_refs'][0]
    calls = [('data_read_table', {'data_session_id': source_ref['parent_id'], 'side': 'left', 'offset': 0, 'limit': 50}),
             ('data_read_table', {'data_session_id': source_ref['parent_id'], 'side': 'right', 'offset': 0, 'limit': 50}),
             ('data_read_result', {'data_session_id': source_ref['parent_id'], 'result_id': source_ref['object_id'], 'offset': 0, 'limit': 50}),
             ('audit_read_evidence', {'evidence_id': source_report['evidence']['data']['evidence_id']})]
    receipts = []
    try:
        for position, (name, arguments) in enumerate(calls):
            receipt = await registry.dispatch({'call_id': 'coldRead' + str(position), 'tool': name, 'arguments': arguments}, context)
            require(receipt['status'] == 'success', 'Restored registered read tool failed: ' + name + ' ' + str(receipt.get('error')))
            receipts.append({'tool': name, 'arguments': arguments, 'actual_receipt': receipt})
        verify_ref(source_ref, context)
        require(receipts[2]['actual_receipt']['result_refs'][0]['sha256'] == source_ref['sha256'], 'Restored data reference content changed')
        require(receipts[3]['actual_receipt']['data']['snapshot_hash'] == source_report['evidence']['data']['snapshot_hash'], 'Restored audit evidence changed')
        frozen_json(output / 'restored-registered-data-read.json', receipts)
        store.status(run['id'], run['generation'], 'completed')
        require(not store.rows('SELECT id FROM model_requests WHERE run_id=?', (run['id'],)), 'Read-only verification unexpectedly called a controller')
        return {'registered_tools': [name for name, _ in calls], 'successful_reads': len(receipts),
            'data_reference_sha256': source_ref['sha256'], 'evidence_snapshot_hash': source_report['evidence']['data']['snapshot_hash'],
            'controller_calls': 0, 'model_calls': 0, 'recalculation_calls': 0, 'verification_run_id': run['id']}
    finally:
        app.state.batch_service.close()
        service.close()


def download_artifacts(root, data_python):
    app, service = platform_services(root, data_python)
    center = app.state.artifact_center
    result = []
    try:
        with closing(sqlite3.connect(center.store.db_path.as_uri() + '?mode=ro', uri=True)) as db:
            entries = db.execute('SELECT workspace_id,artifact_id FROM published_artifact ORDER BY artifact_id').fetchall()
        for workspace, identifier in entries:
            artifact = center.store.get(workspace, identifier)
            with center.store.stage_download(workspace, artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                require(fpath(path).is_relative_to(root) and digest(path) == artifact.blob_hash, 'Restored artifact download escaped/new SHA differs')
                result.append({'artifact_id': identifier, 'revision': artifact.revision, 'sha256': digest(path), 'bytes': path.stat().st_size})
        require(result, 'Actual cold restored artifacts are missing')
        return result
    finally:
        app.state.batch_service.close()
        service.close()


async def execute(args):
    output, source_run, s07 = [fpath(value) for value in (args.output, args.source_run, args.s07_run)]
    require(not output.exists(), 'Use a new evidence output directory')
    lineage = read_json(s07 / 's07-native-lineage.json')
    stop = read_json(s07 / 'owned-read-service-stop.json')
    independent = read_json(args.s07_independent)
    require(fpath(lineage['source_run']) == source_run and stop['owned_process_exited'] and stop['source_run_files_unchanged'],
            'S07 copied source/service closure qualification missing')
    for name, expected in independent['bindings'].items():
        require(digest(s07 / name) == expected, 'S07 independent qualification evidence changed')
    before = {'upstream': inventory(source_run), 's07': inventory(s07)}
    require(before['upstream'] == lineage['source_run_files_before'], 'Original run differs from S07 frozen source inventory')
    output.mkdir(parents=True)
    for env in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME', 'MPLCONFIGDIR'):
        path = output / 'environment' / env.lower(); path.mkdir(parents=True); os.environ[env] = str(path)
    os.environ['WORKBENCH_AGENT_LAYER'] = str(fpath(args.agent_layer))
    invocation = {'command_argv': list(sys.orig_argv), 'cwd': str(fpath(Path.cwd())),
        'runtime': {name: {'path': str(fpath(value)), 'sha256': digest(value)} for name, value in
                    (('platform', sys.executable), ('native', args.native_python), ('data', args.data_python))},
        'python_version': sys.version, 'driver_sha256': digest(__file__),
        'run_environment': {name: os.environ.get(name) for name in
            ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME', 'MPLCONFIGDIR', 'WORKBENCH_AGENT_LAYER')},
        'shared_dependencies': {'bundle': str(fpath(args.bundle)), 'pdf_layer': str(fpath(args.pdf_layer)),
            'agent_layer': str(fpath(args.agent_layer))},
        'helpers': {path.name: digest(path) for path in (Path(__file__), Path(__file__).with_name('common.py'),
            Path(__file__).with_name('s07_native_read.py'))}}
    frozen_json(output / 'actual-invocation.json', invocation)
    consumers = freeze_consumers(args, output)
    platform = fpath(args.platform_source)
    sys.path[:0] = [str(platform), str(platform / 'src')]
    from scripts.backup_workspace import create, verify, restore, _relocate_screen_paths
    from scripts.product_compatibility import check, DATA_COMPATIBILITY
    frozen_json(output / 'original-sources-before.json', before)
    report = {'format': 'clear-agent-screen-cold-restore-v1', 'status': 'running', 'scope_inputs': 12,
        'actual_screen_data': True, 'controller_calls': 0, 'model_calls': 0, 'gpu_calls': 0,
        'source_run': str(source_run), 's07_run': str(s07), 'driver_sha256': digest(__file__),
        'consumer_source_inventory': {'path': str(output / 'consumer-source-inventory.json'),
            'sha256': digest(output / 'consumer-source-inventory.json'), 'inventory_sha256': consumers['inventory_sha256']},
        'actual_invocation': {'path': str(output / 'actual-invocation.json'), 'sha256': digest(output / 'actual-invocation.json')},
        'scope': 'Actual engineering 12-image simulation and actual registered data/evidence outputs; not 240/24 quality or real-photo accuracy'}
    try:
        sources, restored, backups = output / 'sources', output / 'restored', output / 'backups'
        main, followup = sources / 'main', sources / 's07'
        mapping = {'assets.sqlite3': 'assets.sqlite3', 'workspaces.sqlite3': 'workspaces.sqlite3',
            'ocr-handoff.sqlite3': 'ocr-handoffs.sqlite3', 'screen-batches.sqlite3': 'screen-batches.sqlite3',
            'blobs': 'asset-blobs', 'artifacts': 'artifact-center', 'agent': 'agent',
            'inputs': 'asset-uploads/replay'}
        migrations = {'main': copy_actual(source_run, main, mapping)}
        s07_mapping = {**mapping, 'screen-batches-reference.sqlite3': 'screen-batches.sqlite3',
            'data': 'data-workbench', 'audit': 'audit', 'batches': 'batches', 'uploads': 'asset-uploads'}
        s07_mapping.pop('screen-batches.sqlite3'); s07_mapping.pop('inputs')
        migrations['s07'] = copy_actual(s07 / 'platform', followup, s07_mapping)
        migrations['s07_inputs'] = copy_actual(source_run, followup, {'inputs': 'asset-uploads/replay'})
        migrations['s07_reference'] = copy_actual(s07, followup, {'reference': 'asset-uploads/reference'})
        native = main / 'native/ocr'; native.parent.mkdir(parents=True)
        shutil.copytree(s07 / 'native/ocr', native)
        require(inventory(native) == inventory(s07 / 'native/ocr'), 'Full native cold copy differs from actual S07 source')
        migrations['native'] = {'source': str(s07 / 'native/ocr'), 'target': str(native), 'files_sha256': inventory(native)}
        require(inventory(source_run) == before['upstream'] and inventory(s07) == before['s07'], 'Original sources changed during exact copies')
        checkpoints = checkpoint_owned(sources)
        native_members = {name: {'sha256': value, 'sqlite': name.endswith('.sqlite3')}
                          for name, value in inventory(native).items() if not name.endswith(('-wal', '-shm'))}
        relocation = _relocate_screen_paths(native, native, {'kind': 'ocr',
            'source_data_dir': str(source_run / 'native/ocr'), 'files': native_members})
        for copied in (main, followup):
            with closing(sqlite3.connect(copied / 'agent/operations.sqlite3')) as db, db:
                for asset_id, path, expected in db.execute('SELECT asset_id,provenance_path,provenance_hash FROM derived_inputs').fetchall():
                    target = copied / 'agent/bridges' / Path(path).name
                    require(target.is_file() and digest(target) == expected, 'Actual derived provenance copy missing')
                    db.execute('UPDATE derived_inputs SET provenance_path=? WHERE asset_id=?', ('bridges/' + target.name, asset_id))
        saver = await initialize_saver(followup)
        pref = preferences_api(main, fpath(args.data_python), save_if_absent=True)
        preferences_api(followup, fpath(args.data_python))
        business_edit=None
        if args.exercise_business_metadata:
            business_edit=await asyncio.to_thread(read_native,args,native,output,'business-edit')
            require(read_json(output/'business-edit-native-stop.json')['owned_process_exited'],
                    'Business edit reader still active before source qualification')
        source_native = await asyncio.to_thread(read_native, args, native, output, 'source')
        require(read_json(output / 'source-native-stop.json')['owned_process_exited'], 'Source reader still active at cold backup')
        checkpoint_owned(sources)
        frozen_json(output / 'copy-path-migrations.json', {'byte_copy': migrations, 'owned_wal_recovery': checkpoints,
            'native_live_path_relocation': relocation, 's07_checkpoint_storage_initialization': saver,
            'preference_api': pref['api_receipts'], 'old_frozen_snapshots_unchanged': pref['old_frozen_snapshots_unchanged']})
        source_state = database_state(sources)
        frozen_json(output / 'cold-source-logical-state.json', source_state)
        cold_files = inventory(sources)
        components = {}
        for name, copied, kind, destination in (
                ('platform_main', main, 'platform', restored / 'main'),
                ('platform_s07', followup, 'platform', restored / 's07'),
                ('ocr', native, 'ocr', restored / 'main/native/ocr')):
            backup = backups / name
            manifest = create(copied, backup, kind=kind)
            require(verify(backup) == manifest, 'Actual backup verification differs')
            receipt = restore(backup, destination)
            components[name] = {'source': str(copied), 'backup': str(backup), 'restored': str(destination),
                'files': len(manifest['files']), 'manifest_sha256': digest(backup / 'manifest.json'), 'actual_restore_receipt': receipt}
        require(inventory(sources) == cold_files, 'Cold source files changed during actual backup/restore')
        isolated_sources = output / 'isolated-cold-sources'
        require(not isolated_sources.exists(), 'Owned cold isolation destination already exists')
        sources.rename(isolated_sources)
        require(not sources.exists() and inventory(isolated_sources) == cold_files,
                'Owned cold source isolation changed source contents')
        frozen_json(output / 'actual-cold-source-isolation.json', {
            'old_owned_source_root': str(sources), 'isolated_owned_source_root': str(isolated_sources),
            'former_source_root_absent_during_restored_consumption': True,
            'original_upstream_and_s07_not_moved': True, 'files_and_sha256_unchanged': True,
            'method': 'Rename only this run owned cold source scope after all services stop; block reads to old scopes'})
        restored_state = database_state(restored)
        require(source_state == restored_state, 'Restored business tables/candidates/versions/preferences/graph snapshots differ')
        frozen_json(output / 'restored-logical-state-before-consumers.json', restored_state)
        check(restored / 'main', DATA_COMPATIBILITY); check(restored / 's07', DATA_COMPATIBILITY)
        restored_native = await asyncio.to_thread(read_native, args, restored / 'main/native/ocr', output, 'restored')
        require(source_native['version_vector'] == restored_native['version_vector']
                and source_native['details'] == restored_native['details'] and source_native['policy'] == restored_native['policy']
                and source_native['source_and_prepared_files'] == restored_native['source_and_prepared_files']
                and source_native['existing_export_files'] == restored_native['existing_export_files'],
                'Actual restored native API identities/files differ')
        restored_pref = preferences_api(restored / 'main', fpath(args.data_python))
        require(pref['preview'] == restored_pref['preview'] and pref['frozen_snapshots'] == restored_pref['frozen_snapshots'],
                'Actual restored preference API or frozen snapshot differs')
        data_read = await read_data(restored / 's07', fpath(args.data_python), read_json(s07 / 's07-result.json'), output)
        artifacts = {name: download_artifacts(restored / name, fpath(args.data_python)) for name in ('main', 's07')}
        protected = database_state(restored)['main/native/ocr/workbench.sqlite3']
        require(protected == restored_state['main/native/ocr/workbench.sqlite3'], 'Read-only consumer changed actual native business tables')
        require(inventory(isolated_sources) == cold_files, 'Restored consumers altered isolated cold source files')
        require(inventory(output / 'consumers') == consumers['files_sha256'], 'Tested consumer source bytes changed during actual run')
        report.update(status='passed', source=str(main), restored=str(restored / 'main'),
            source_services_stopped=True, backup_verified=True, restored_artifact_downloads_verified=True,
            source_version_vector=source_native['version_vector'], restored_version_vector=restored_native['version_vector'],
            source_preferences={key: pref[key] for key in ('preview', 'frozen_snapshots')},
            restored_preferences={key: restored_pref[key] for key in ('preview', 'frozen_snapshots')},
            preference_api_receipts={'source': pref['api_receipts'], 'restored': restored_pref['api_receipts']}, components=components,
            actual_registered_data_read=data_read, artifact_downloads=artifacts,
            policy_preserved=True, candidates_revisions_review_state_preserved=True, logical_state_equal=True,
            business_metadata_edit_restore_verified=business_edit is not None,
            business_metadata_edit_probe=business_edit['business_metadata_probe'] if business_edit else None,
            actual_cold_source_isolation={'path': str(output / 'actual-cold-source-isolation.json'),
                'sha256': digest(output / 'actual-cold-source-isolation.json'), 'isolated_source': str(isolated_sources)},
            actual_native_consumer_reads={'source': source_native['actual_read_audit'], 'restored': restored_native['actual_read_audit']},
            actual_diagnostic_clock_verification={phase: value['actual_diagnostic_clock_verification']
                for phase, value in (('source', source_native), ('restored', restored_native))},
            diagnostics_comparison={'equal': True, 'only_normalized_field': 'execution_budget.wall_deadline_remaining_seconds',
                'reason': 'Live deadline minus real request time is verified against unchanged persisted deadline; raw responses retained'},
            diagnostics_version='screen-diagnostics-v2', consumer_source_bytes_unchanged=True,
            source_files_during_backup_unchanged=True, old_frozen_snapshots_unchanged=True,
            limits=['Actual 12-image engineering data cold recovery; does not qualify full 240/24 workflow',
                'Default output preference saved only in an authorized new copied scope if absent upstream',
                'S07 had deterministic tool history, so only an empty production checkpoint store is initialized there',
                'No OS crash/power failure, inference, autonomous planning or human signoff is claimed'])
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        after = {'upstream': inventory(source_run), 's07': inventory(s07)}
        report['original_source_files_and_sha_unchanged'] = before == after
        frozen_json(output / 'original-sources-after.json', after)
        if before != after:
            report.update(status='failed', error='Original source file set/SHA changed')
        frozen_json(output / 'cold-restore-result.json', report)
    require(report['status'] == 'passed', 'Actual cold restore failed; inspect preserved report')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--exercise-business-metadata',action='store_true',
        help='Persist an unconfirmed engineering metadata edit in the owned source copy before cold backup')
    for name in ('output', 'source-run', 's07-run', 's07-independent', 'platform-source',
                 'native-source', 'bundle', 'pdf-layer', 'data-python', 'native-python',
                 'agent-layer', 'native-data', 'token-file', 'read-audit-log', 'reader-runtime-report'):
        parser.add_argument('--' + name)
    parser.add_argument('--forbid-data-root', action='append', default=[])
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    if args.serve:
        serve(args)
    else:
        report = asyncio.run(execute(args))
        print(json.dumps({key: report[key] for key in ('status', 'scope_inputs', 'original_source_files_and_sha_unchanged',
            'logical_state_equal', 'controller_calls', 'model_calls', 'gpu_calls')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
