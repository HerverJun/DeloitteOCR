"""Actual native OCR -> registered data/evidence tools on an isolated cold copy."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import closing
import csv
import http.client
import io
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from common import FORMAT, digest, fpath, frozen_bytes, frozen_json, read_json
from native_bridge import collect
from s07_native_followup import dispatch, run_tools


def inventory(root):
    output = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Original native evidence contains linked files/directories')
        if path.is_file():
            if not fpath(path).is_relative_to(root):
                raise ValueError('Original native evidence escaped the F: run root')
            output[path.relative_to(root).as_posix()] = digest(path)
    return output


def cold_copy(source, destination):
    members = [source, source.with_name(source.name + '-wal'), source.with_name(source.name + '-shm')]
    before = {path.name: digest(path) for path in members if path.exists()}
    for original in members:
        if original.exists():
            target = destination.with_name(destination.name + original.name[len(source.name):])
            frozen_bytes(target, original.read_bytes())
            if digest(target) != before[original.name]:
                raise ValueError('Isolated database/WAL/SHM bytes differ from the captured source')
    after = {path.name: digest(path) for path in members if path.exists()}
    if before != after:
        raise ValueError('Original database/WAL/SHM changed during isolated byte copy')
    return {'source': str(source), 'captured_files': before,
            'method': 'Exact stable DB and WAL/SHM byte copy; SQLite recovery/checks occur only in the isolated destination'}


def verify_native_copy(database, expected_identity):
    from native_bridge import ADAPTER
    spec = importlib.util.spec_from_file_location('s07_copy_identity_adapter', ADAPTER)
    adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Isolated native database/WAL snapshot failed SQLite integrity')
    actual_identity = adapter.table_identity(adapter.read_database(database))
    if actual_identity != expected_identity:
        raise ValueError('Isolated native database/WAL committed tables differ from original actual identity')
    return {'native_table_identity_sha256': actual_identity, 'sqlite_integrity': 'ok',
            'source_opened_for_recovery': False, 'qualification_scope': 'Before isolated create_app initialization'}


def load_image_runtime(native_python):
    """Use the installed same-ABI image package without shadowing platform packages."""
    layer = fpath(native_python).parent / 'Lib/site-packages'
    spec = importlib.machinery.PathFinder.find_spec('PIL', [str(layer)])
    if spec is None or spec.origin is None or not fpath(spec.origin).is_relative_to(layer):
        raise ValueError('The native reader has no F: installed Pillow package')
    loaded = sys.modules.get('PIL')
    if loaded is not None and fpath(loaded.__file__) != fpath(spec.origin):
        raise ValueError('Unexpected preloaded image package identity')
    if loaded is None:
        loaded = importlib.util.module_from_spec(spec)
        sys.modules['PIL'] = loaded
        spec.loader.exec_module(loaded)
    return {'module': 'PIL', 'version': loaded.__version__, 'path': str(fpath(loaded.__file__)),
            'sha256': digest(loaded.__file__), 'role': 'Original-image pixel identity only; no OCR model'}


def native_http(base, token, path):
    parsed = urlsplit(base)
    if parsed.hostname != '127.0.0.1' or not parsed.port:
        raise ValueError('S07 native reader must use its owned loopback service')
    client = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        client.request('GET', path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
        response = client.getresponse(); payload = response.read(16 * 1024 * 1024 + 1)
        if response.status != 200 or len(payload) > 16 * 1024 * 1024:
            raise ValueError('Actual disabled-queue native reader did not return a bounded successful reply')
        return json.loads(payload)
    finally:
        client.close()


def serve(args):
    source = fpath(args.native_source)
    sys.path[:0] = [str(source / 'src'), str(fpath(args.pdf_layer).parent)]
    from ocr_workbench.service import create_app
    import uvicorn
    app = create_app(fpath(args.bundle), fpath(args.native_data), fpath(args.token_file).read_text('utf-8'),
                     start_queue=False, agent_enabled=False)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


def platform_services(root, source_run, base, token, process, data_python):
    from fastapi import FastAPI
    from workbench_platform.store import WorkspaceStore
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetStore
    from workbench_platform.bridge.context import ResolvedContext
    from workbench_platform.jobs.ocr_handoff import OcrHandoff
    from workbench_platform.artifacts.center import ArtifactCenter
    from workbench_platform.data_api.service import DataWorkbenchService
    from workbench_platform.batches.service import BatchService
    from workbench_platform.audit.service import AuditService
    from workbench_platform.agent.store import AgentStore
    from workbench_platform.agent.tasks import AuditTasks
    platform = root / 'platform'; platform.mkdir()
    for name in ('assets.sqlite3', 'ocr-handoff.sqlite3', 'workspaces.sqlite3'):
        cold_copy(source_run / name, platform / name)
    (platform / 'blobs').mkdir(); (platform / 'uploads').mkdir()
    if (source_run / 'blobs').exists():
        for original in (source_run / 'blobs').rglob('*'):
            if original.is_file(): frozen_bytes(platform / 'blobs' / original.relative_to(source_run / 'blobs'), original.read_bytes())
    workspaces = WorkspaceStore(platform / 'workspaces.sqlite3')
    assets = AssetRegistry(platform / 'assets.sqlite3', platform / 'blobs',
        {'replay': source_run / 'inputs', 'uploads': platform / 'uploads', 's07reference': root / 'reference'})
    sets = AssetSetStore(assets)
    db_path = platform / 'screen-batches-reference.sqlite3'
    cold_copy(source_run / 'screen-batches.sqlite3', db_path)
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('SELECT * FROM screen_batch_refs')]
    if len(rows) != 1:
        raise ValueError('S07 requires one actual native batch identity, not an invented mapping')
    batch = rows[0]
    links = [link for link in workspaces.list_links(batch['workspace_id']) if link.app_id == 'ocr'
             and link.module_project_id == batch['project_id']]
    if len(links) != 1:
        raise ValueError('Cold-copied workspace has no unique actual OCR project link')
    link = links[0]
    context = ResolvedContext(batch['workspace_id'], 'ocr', 's07OwnedNativeReader', link.link_id,
        link.module_project_id, link.role, '/apps/ocr/workflow', base)
    def bound(workspace, family, link_id):
        if (workspace, family, link_id) != (context.workspace_id, 'ocr', context.link_id):
            raise ValueError('S07 native adapter context does not match copied actual project scope')
        return context
    jobs = SimpleNamespace(workspaces=workspaces, _context=bound, _token=lambda: token,
                           _live=lambda value: value == context and process.poll() is None)
    handoff = OcrHandoff(platform / 'ocr-handoff.sqlite3', jobs, assets, sets)
    original = handoff._row(context.workspace_id, batch['handoff_id'])
    fixed = sets.get(context.workspace_id, original['asset_set_id'], original['asset_set_revision'])
    data = DataWorkbenchService(platform / 'data', data_python); data.asset_registry = assets
    center = ArtifactCenter(platform / 'artifacts', assets, sets, data, ocr_handoff=handoff)
    value = SimpleNamespace(workspace_store=workspaces, asset_library=(assets, sets, None),
        job_service=jobs, ocr_handoff=handoff, data_workbench=data, artifact_center=center)
    app = FastAPI(); app.state = value
    value.batch_service = BatchService(app, platform / 'batches'); value.batch_store = value.batch_service.store
    value.audit_service = AuditService(app, platform / 'audit')
    store = AgentStore(platform / 'agent')
    value.agent = SimpleNamespace(store=store, services=value); value.agent.tasks = AuditTasks(value.agent)
    return value, store, batch, fixed


def reference_file(truth_path, inputs_path, sample_id, table_index, target):
    freeze_path = truth_path.parent / 'freeze.json'
    freeze = read_json(freeze_path)
    if (digest(truth_path) != freeze['truth_sha256']
            or digest(inputs_path) != freeze['inputs_sha256']
            or digest(truth_path.parent / 'inputs.json') != freeze['inputs_sha256']):
        raise ValueError('Independent reference truth/input freeze does not match actual model input')
    truths = read_json(truth_path)
    samples = [sample for sample in truths['samples'] if sample['id'] == sample_id]
    if len(samples) != 1:
        raise ValueError('Explicit simulated reference sample is absent/ambiguous')
    input_samples = [sample for sample in read_json(inputs_path)['samples'] if sample['id'] == sample_id]
    if len(input_samples) != 1 or input_samples[0]['image_sha256'] != samples[0]['image_sha256']:
        raise ValueError('Independent reference source does not match the actual OCR image')
    table = samples[0]['tables'][table_index]
    if any(cell.get('row_span', 1) != 1 or cell.get('column_span', 1) != 1 for cell in table['cells']):
        raise ValueError('Reference ledger has merge semantics; no arbitrary expansion is permitted')
    positions = {(cell['row'], cell['column']): cell['text'] for cell in table['cells']}
    if len(positions) != table['rows'] * table['columns']:
        raise ValueError('Explicit reference ledger does not have a complete literal grid')
    stream = io.StringIO(newline='')
    csv.writer(stream).writerows([[positions[(row, column)] for column in range(table['columns'])]
                                 for row in range(table['rows'])])
    frozen_bytes(target, stream.getvalue().encode('utf-8-sig'))
    return {'source': str(truth_path), 'sha256': digest(truth_path), 'sample_id': sample_id,
            'freeze_sha256': digest(freeze_path), 'actual_inputs_sha256': digest(inputs_path),
            'image_sha256': samples[0]['image_sha256'], 'reference_csv_sha256': digest(target),
            'table_index': table_index, 'scope': 'Independent simulated reference used only after actual OCR inference; not a model input or cell repair oracle'}


async def execute(args):
    root, source_run = fpath(args.output_root), fpath(args.native_run)
    if root.exists():
        raise ValueError('S07 native followup requires a new isolated output root')
    root.mkdir(parents=True)
    original_files = inventory(source_run)
    image_runtime = load_image_runtime(args.native_python)
    index, observations = collect(source_run, fpath(args.inputs))
    frozen_json(root / 'original-native-index.json', index)
    frozen_json(root / 'image-reader-runtime.json', image_runtime)
    prepared = read_json(source_run / 'prepared.json')
    source, bundle = fpath(prepared['native_source']), fpath(prepared['bundle'])
    if digest(args.inputs) != prepared['inputs_sha256']:
        raise ValueError('S07 native input freeze changed')
    native = root / 'native/ocr'
    native.parent.mkdir()
    shutil.copytree(source_run / 'native/ocr', native)
    if inventory(native) != inventory(source_run / 'native/ocr'):
        raise ValueError('Full isolated native DB/files copy differs from actual source')
    copy_receipt = cold_copy(source_run / 'native/ocr/workbench.sqlite3', native / 'workbench.sqlite3')
    copy_qualification = verify_native_copy(native / 'workbench.sqlite3', index['native_table_identity_sha256'])
    frozen_json(root / 'native-copy-qualification.json', {'format': FORMAT,
        'byte_copy': copy_receipt, 'logical_snapshot': copy_qualification})
    if inventory(source_run) != original_files:
        raise ValueError('Original native evidence changed during cold-copy preparation')
    reference = reference_file(fpath(args.truth), fpath(args.inputs), args.sample_id, args.table_index,
                               root / 'reference/reference.csv')
    token_path = root / 'owned-reader-token.txt'; token_path.write_text(secrets.token_urlsafe(36), encoding='utf-8')
    token = token_path.read_text('utf-8')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    base = 'http://127.0.0.1:' + str(port)
    command = [str(fpath(args.native_python)), '-B', '-c',
        'import runpy,sys;from pathlib import Path;p=Path(sys.argv[2]).resolve();sys.path.insert(0,sys.argv[1]);sys.argv=[str(p),*sys.argv[3:]];runpy.run_path(str(p),run_name="__main__")',
        str(Path(__file__).parent.resolve()), str(Path(__file__).resolve()), '--serve',
        '--native-source', str(source), '--bundle', str(bundle), '--native-data', str(native),
        '--token-file', str(token_path), '--pdf-layer', str(fpath(args.pdf_layer)), '--port', str(port)]
    value = None; process = None; start = time.perf_counter()
    with (root / 'native-reader.log').open('wb') as log:
        try:
            process = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.perf_counter() + 60
            while time.perf_counter() < deadline:
                if process.poll() is not None: raise ValueError('Owned disabled-queue native reader exited; inspect its F: log')
                try:
                    state = await asyncio.to_thread(native_http, base, token, '/api/state')
                    break
                except (OSError, ValueError): await asyncio.sleep(.2)
            else: raise ValueError('Owned disabled-queue native reader did not become readable')
            if any((state.get(name) or {}).get('alive') for name in ('queue', 'fusion_queue', 'external_queue')):
                raise ValueError('S07 native reader unexpectedly started a calculation queue')
            platform = fpath(args.platform_source); sys.path[:0] = [str(platform), str(platform / 'src')]
            from packages.contracts.agent import AgentRef
            from workbench_platform.agent.registry import ToolContext, ToolRegistry
            from workbench_platform.agent.tools import results, data, audit
            value, store, batch, fixed = platform_services(root, source_run, base, token, process, fpath(args.data_python))
            reference_asset = value.asset_library[0].register(batch['workspace_id'], 's07reference', 'reference.csv',
                                                            media_type='text/csv', storage_mode='managed')
            selected = [member.asset_id for member in fixed.items] + [reference_asset.asset_id]
            session = store.create_session(batch['workspace_id'], 'S07真实OCR结果受控衔接', selected, 's07NativeSession')
            rule = '工程模拟核对：按Loci严格匹配Gene原文；不推断科学/金额单位，不改OCR值、不签人工确认。'
            run, _ = store.start_run(session['id'], rule, 's07NativeRun', 0)
            value.agent.tasks.record_goal(session['id'], rule)
            ctx = ToolContext(batch['workspace_id'], session['id'], run['id'], run['generation'], 0, '', tuple(selected), value, store)
            registry = ToolRegistry(); results.register(registry); data.register(registry); audit.register(registry)
            adapter = value.ocr_handoff._adapter(value.ocr_handoff._context(batch['workspace_id'], batch['link_id'] if 'link_id' in batch else value.ocr_handoff._row(batch['workspace_id'], batch['handoff_id'])['link_id']))
            traces = []; bridge_outcomes = []; bridged = None
            for row in index['rows']:
                if row['scoring_status'] != 'success': continue
                artifacts = adapter.results([row['stored_result_task']['id']])
                if len(artifacts) != 1 or artifacts[0].native_result_id != row['result_id']:
                    raise ValueError('Actual disabled-queue adapter result differs from original native identity')
                table_snapshot = adapter.result_tables(artifacts[0], row['result_revision'])
                ref = AgentRef(kind='result', workspace_id=batch['workspace_id'], object_id=row['result_id'],
                    revision=row['result_revision'], parent_id=batch['handoff_id'], application='ocr',
                    native_id=row['result_id'], sha256=table_snapshot['content_sha256'])
                described = await dispatch(registry, ctx, 'results_describe', {'reference': ref.model_dump()}, traces,
                                           call_prefix='s07NativeDescribe')
                call = {'call_id': 's07NativeBridge' + str(len(bridge_outcomes) + 1), 'tool': 'results_to_input',
                        'arguments': {'reference': ref.model_dump(), 'table_index': args.table_index}}
                result = await registry.dispatch(call, ctx)
                bridge_outcomes.append({'sample_id': row['id'], 'reference': ref.model_dump(),
                                        'actual_catalog': described['data'], 'actual_receipt': result})
                if result['status'] not in {'success', 'unsupported', 'error'}:
                    raise ValueError('Unexpected actual result bridge lifecycle')
                if row['id'] == args.sample_id:
                    if result['status'] != 'success':
                        raise ValueError('Explicit native sample is not bridgeable; no downstream input invented')
                    bridged = result['data']
            frozen_json(root / 'native-bridge-outcomes.json', bridge_outcomes)
            frozen_json(root / 'native-describe-traces.json', traces)
            if bridged is None: raise ValueError('Explicit native result sample was not actually observed')
            report = await run_tools(registry, ctx, bridged, {'asset_id': reference_asset.asset_id,
                'asset_revision': reference_asset.revision, 'blob_hash': reference_asset.blob_hash},
                key_header='Loci', compare_headers=['Gene'], amount_headers=[], rule=rule, output=root,
                origin_scope='Actual native OCR result -> existing results_to_input -> registered CPU data comparison -> direct audit evidence; no new model/controller calls')
            store.status(run['id'], run['generation'], 'completed')
            report_path = root / 's07-result.json'
            frozen_json(root / 's07-native-lineage.json', {'format': FORMAT, 'status': 'actual_native_bridge_completed',
                'source_run': str(source_run), 'native_index_sha256': digest(root / 'original-native-index.json'),
                'native_source': str(source), 'native_source_inventory_sha256': index['native_source_inventory_sha256'],
                'source_run_files_before': original_files, 'reference': reference, 'actual_followup_result': str(report_path),
                'actual_followup_result_sha256': digest(report_path), 'disabled_reader': {'pid': process.pid,
                    'start_queue': False, 'agent_enabled': False, 'base_url': base},
                'scope': 'Separate cold-copy actual native/tool followup, not included in upstream model/Agent timing or rounds',
                'wall_seconds_before_owned_shutdown': time.perf_counter() - start, 'model_calls': 0, 'controller_calls': 0,
                'bridge_counts': {status: sum(entry['actual_receipt']['status'] == status for entry in bridge_outcomes)
                                 for status in ('success', 'unsupported', 'error')}})
            return report
        except Exception as error:
            frozen_json(root / 's07-native-failure.json', {'format': FORMAT, 'status': 'actual_native_followup_failed',
                'error': str(error), 'scope': 'Actual cold-copy native service/tool run; original inference not rerun',
                'model_calls': 0, 'controller_calls': 0})
            raise
        finally:
            try:
                if value is not None:
                    try: value.batch_service.close()
                    finally: value.data_workbench.close()
            finally:
                try:
                    if process is not None and process.poll() is None:
                        process.terminate(); process.wait(timeout=10)
                finally:
                    unchanged = inventory(source_run) == original_files
                    frozen_json(root / 'owned-read-service-stop.json', {'format': FORMAT,
                        'pid': process.pid if process else None, 'owned_process_exited': process is None or process.poll() is not None,
                        'source_run_files_unchanged': unchanged, 'source_run': str(source_run),
                        'model_calls': 0, 'controller_calls': 0})
                    if not unchanged: raise ValueError('Original native evidence changed during cold-copy followup')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    for name in ('native-source', 'bundle', 'native-data', 'token-file', 'pdf-layer', 'native-run', 'inputs',
                 'truth', 'sample-id', 'platform-source', 'data-python', 'native-python', 'output-root'):
        parser.add_argument('--' + name)
    parser.add_argument('--port', type=int, default=0); parser.add_argument('--table-index', type=int, default=0)
    args = parser.parse_args()
    if args.serve: serve(args)
    else:
        try:
            result = asyncio.run(execute(args))
        except Exception as error:
            root = fpath(args.output_root)
            if root.exists() and not (root / 's07-native-failure.json').exists():
                frozen_json(root / 's07-native-startup-failure.json', {'format': FORMAT,
                    'status': 'native_followup_initialization_failed', 'error': str(error),
                    'model_calls': 0, 'controller_calls': 0,
                    'scope': 'Reader/identity preparation failed; no inference rerun'})
            raise
        print(result['status'], result['scope'])
