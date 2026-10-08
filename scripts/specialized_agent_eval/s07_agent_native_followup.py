"""Real production Agent over existing native OCR results and CPU data/evidence.

Preparation is the default. --run-controller authorizes the configured native
controller on a new F: scope. No provider stub or precomputed tool reply exists.
The OCR reader is always a complete isolated DB/WAL/files copy with no queues.
"""
from __future__ import annotations
import argparse
import asyncio
import csv
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from common import FORMAT, digest, fingerprint, fpath, frozen_json, read_json
from native_bridge import collect
from s07_native_read import (cold_copy, inventory, load_image_runtime, native_http,
                             platform_services, reference_file, verify_native_copy)


BUSINESS_TOOLS = frozenset({
    'native_attach_result', 'native_read_result', 'results_describe', 'results_to_input',
    'data_create_session', 'data_inspect_input', 'data_import', 'data_read_table',
    'data_read_result', 'data_compare', 'data_export', 'audit_bind_evidence', 'audit_read_evidence',
})
DISCOVERY_TOOLS = frozenset({
    'conversation_answer', 'delivery_publish', 'tools_list', 'tools_describe',
    'workspace_get_context', 'assets_search', 'assets_describe', 'artifacts_get', 'models_list', 'audit_get_task', 'audit_get_project',
    'ask_user', 'session_read_history', 'preferences_read', 'capabilities_read',
    'skills_list', 'skills_activate', 'skills_read_resource', 'skills_deactivate',
})


def configure(args):
    root = fpath(args.output_root)
    if root.exists():
        raise ValueError('Use a new isolated output root; Agent evidence is immutable')
    root.mkdir(parents=True)
    temporary = root / 'temp'; temporary.mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME', 'MPLCONFIGDIR'):
        os.environ[name] = str(temporary)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['WORKBENCH_AGENT_LAYER'] = str(fpath(args.agent_layer))
    os.environ['WORKBENCH_AGENT_ROUND_LIMIT'] = '20'
    os.environ['WORKBENCH_AGENT_TOOL_LIMIT'] = '60'
    source = fpath(args.platform_source)
    sys.path[:0] = [str(source), str(source / 'src')]
    return root


def platform_binding(source):
    files = sorted((source / 'src').rglob('*.py')) + sorted((source / 'packages').rglob('*.py'))
    files += sorted((source / 'modules').rglob('*.py'))
    files += sorted((source / 'config').rglob('*.json')) + sorted((source / 'config').rglob('*.md'))
    files += [source / 'scripts/evaluate_screen_agent_replay.py']
    if any(path.is_symlink() or path.is_junction() for path in files):
        raise ValueError('Frozen Agent platform source contains linked resources')
    return {path.relative_to(source).as_posix(): digest(path) for path in files}


def prepare(args, root):
    source_run = fpath(args.native_run)
    before = inventory(source_run)
    image_runtime = load_image_runtime(args.native_python)
    index, _ = collect(source_run, fpath(args.inputs))
    if inventory(source_run) != before:
        raise ValueError('Original inference evidence changed during read-only preparation')
    selected = [row for row in index['rows'] if row['id'] == args.sample_id and row['scoring_status'] == 'success']
    if len(selected) != 1:
        raise ValueError('Explicit existing native result is missing or unsuccessful')
    reference = reference_file(fpath(args.truth), fpath(args.inputs), args.sample_id, args.table_index,
                               root / 'reference/reference.csv')
    original = read_json(source_run / 'prepared.json')
    frozen_json(root / 'original-native-index.json', index)
    binding = {'format': 'specialized-real-agent-native-followup-prepared-v1',
        'native_run': str(source_run), 'native_run_files': before, 'native_prepared_sha256': digest(source_run / 'prepared.json'),
        'inputs': str(fpath(args.inputs)), 'inputs_sha256': digest(args.inputs),
        'sample_id': args.sample_id, 'table_index': args.table_index, 'reference': reference,
        'native_index_sha256': digest(root / 'original-native-index.json'),
        'native_source': original['native_source'], 'bundle': original['bundle'],
        'platform_source': str(fpath(args.platform_source)), 'platform_source_files': platform_binding(fpath(args.platform_source)),
        'image_runtime': image_runtime, 'controller_store': str(fpath(args.controller_store)),
        'controller_settings_or_credentials_displayed': False, 'controller_calls': 0, 'model_calls': 0,
        'native_reader_started': False, 'native_inference_rerun': False,
        'agent_layer': str(fpath(args.agent_layer)), 'allowed_registered_tools': sorted(BUSINESS_TOOLS | DISCOVERY_TOOLS),
        'limits': ['Preparation reads independent simulated reference after original inference; the controller sees only registered reference CSV.',
                   'The 003 direct-tool sample supported 2/12 tables and rejected 10 merged tables; this task selects exactly one supported English engineering table.',
                   'No GPU, training, OCR resubmission, numeric inference, source repair or human signoff is allowed.']}
    frozen_json(root / 'prepared.json', binding)
    return binding, index


def reader_command(args, root, binding, token_path, port):
    script = Path(__file__).with_name('s07_native_read.py').resolve()
    bootstrap = ('import runpy,sys;from pathlib import Path;p=Path(sys.argv[2]).resolve();'
                 'sys.path.insert(0,sys.argv[1]);sys.argv=[str(p),*sys.argv[3:]];runpy.run_path(str(p),run_name="__main__")')
    return [str(fpath(args.native_python)), '-B', '-c', bootstrap, str(script.parent), str(script), '--serve',
        '--native-source', binding['native_source'], '--bundle', binding['bundle'], '--native-data', str(root / 'native/ocr'),
        '--token-file', str(token_path), '--pdf-layer', str(fpath(args.pdf_layer)), '--port', str(port)]


def expose_owned_reader(value, batch, process, base, token, bundle):
    """Harness process binding only; actual HTTP adapters and tool handlers stay intact."""
    from workbench_platform.catalog.launch import LaunchStatus
    row = value.ocr_handoff._row(batch['workspace_id'], batch['handoff_id'])
    context = value.ocr_handoff._context(batch['workspace_id'], row['link_id'])
    value.job_service._adapter = value.ocr_handoff._adapter
    def current(family):
        if family != 'ocr' or process.poll() is not None:
            raise LaunchStatus(410, 'APP_UNAVAILABLE')
        health = native_http(base, token, '/api/health')
        if health.get('queue', {}).get('alive') or health.get('fusion_queue', {}).get('alive'):
            raise LaunchStatus(503, 'APP_UNAVAILABLE')
        return SimpleNamespace(instance_id=context.instance_id, base_url=base, pid=process.pid)
    value.launch_service = SimpleNamespace(current=current)
    # Installation comes from the same existing, checksum-bound native bundle,
    # not a replacement native reply or a production Supervisor qualification.
    installed = (fpath(bundle) / 'config/engines.json').is_file()
    value.supervisor = SimpleNamespace(registry=SimpleNamespace(installations={'ocr': SimpleNamespace(installed=installed)}))
    return context


def validate_agent_result(runtime, value, run, root, expected_table, reference_asset):
    operations = runtime.store.rows('SELECT * FROM operations WHERE run_id=? ORDER BY created', (run['id'],))
    receipts = [{**{key: row[key] for key in ('id', 'call_id', 'tool', 'state', 'created', 'updated')},
        'arguments': json.loads(row['arguments']), 'receipt': json.loads(row['result']) if row.get('result') else None}
        for row in operations]
    success = {entry['tool']: entry for entry in receipts if (entry['receipt'] or {}).get('status') == 'success'}
    required = {'native_attach_result', 'results_describe', 'results_to_input', 'data_create_session', 'data_import', 'data_read_table',
                'data_compare', 'data_read_result', 'audit_bind_evidence', 'audit_read_evidence', 'data_export', 'delivery_publish'}
    missing = sorted(required - set(success))
    checks, files = {}, {}
    if not missing:
        imported = success['results_to_input']['receipt']['data']
        sha = imported['blob_hash']; blobs = list((root / 'platform/blobs').rglob(sha))
        if len(blobs) != 1 or digest(blobs[0]) != sha:
            raise ValueError('Actual Agent-derived CSV has no unique matching managed blob')
        expected = {(cell['row'], cell['column']): cell['text'] for cell in expected_table['cells']}
        with blobs[0].open(encoding='utf-8-sig', newline='') as stream:
            observed = list(csv.reader(stream))
        literal = [[expected[(row, col)] for col in range(expected_table['columns'])] for row in range(expected_table['rows'])]
        if observed != literal:
            raise ValueError('Actual Agent bridge changed original native cell literals')
        compared = success['data_compare']['arguments']
        from decimal import Decimal
        from workbench_platform.agent.tools.data import CompareSpec
        spec = CompareSpec.model_validate(compared['spec']).model_dump()
        if Decimal(spec['amount_tolerance']) == 0:
            spec['amount_tolerance'] = '0'
        expected_spec = {'key_pairs': [['c1', 'c1']], 'compare_pairs': [['c3', 'c3']], 'amount_pairs': [],
                         'type_policy': 'strict', 'null_policy': 'never_match', 'amount_tolerance': '0',
                         'trim_text': False, 'casefold_text': False}
        if spec != expected_spec:
            raise ValueError('Actual Agent comparison rule differs from explicit Loci/Gene original-text contract')
        session = value.data_workbench.get(compared['data_session_id'])
        tables = session['state']['tables']
        if len(tables) != 2:
            raise ValueError('Actual Agent comparison did not retain exactly two imported sources')
        if tables['left']['source']['sha256'] != sha or tables['right']['source']['sha256'] != reference_asset.blob_hash:
            raise ValueError('Actual Agent comparison substituted an unauthorized reference/input')
        evidence = success['audit_read_evidence']['receipt']['data']
        if evidence.get('stale'):
            raise ValueError('Actual Agent evidence is stale')
        if evidence['snapshot_hash'] != success['audit_bind_evidence']['receipt']['data']['snapshot_hash']:
            raise ValueError('Actual Agent evidence readback differs from binding receipt')
        if success['data_export']['arguments'].get('categories'):
            raise ValueError('Explicit sample delivery requires all comparison categories')
        bundle = success['data_export']['receipt']['data']['bundle']
        for entry in bundle['files']:
            artifact = value.artifact_center.file(run['workspace_id'], bundle['bundle_id'], entry['name'])
            with value.artifact_center.store.stage_download(run['workspace_id'], artifact.artifact_id,
                    expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                if digest(path) != artifact.blob_hash:
                    raise ValueError('Actual Agent export differs from its registered SHA')
                target = root / 'delivered' / entry['name']
                from common import frozen_bytes
                frozen_bytes(target, path.read_bytes())
            files[entry['name']] = {'path': str(target), 'sha256': digest(target), 'artifact_id': artifact.artifact_id}
        summary = read_json(root / 'delivered/summary.json')
        if summary['coverage']['same_pairs'] != 6 or summary['coverage']['different_pairs'] != 1:
            raise ValueError('Actual Agent comparison result differs from the original-literal sample')
        with (root / 'delivered/results.csv').open(encoding='utf-8-sig', newline='') as stream:
            differences = [row for row in csv.DictReader(stream) if row['category'] == 'different']
        if (len(differences) != 1 or differences[0]['L_c1_raw'] != 'rs4387287'
                or differences[0]['L_c3_raw'] != 'OBFCI' or differences[0]['R_c3_raw'] != 'OBFC1'):
            raise ValueError('Actual Agent comparison reordered or changed the observed OCR/reference difference')
        delivery = success['delivery_publish']['receipt']['data']
        if not delivery.get('verified'):
            raise ValueError('Actual Agent did not publish a verified delivery')
        if bundle['bundle_id'] not in {item['bundle_id'] for item in delivery.get('delivery', [])}:
            raise ValueError('Actual Agent delivery omits the exported comparison artifact')
        checks = {'native_literal_cells_verified': len(expected), 'actual_compare_spec': spec,
                  'coverage': summary['coverage'], 'evidence_id': evidence['evidence_id'],
                  'evidence_snapshot_hash': evidence['snapshot_hash'], 'verified_delivery': True}
    frozen_json(root / 'actual-agent-operations.json', receipts)
    return missing, checks, files, len(operations)


async def execute(args, root, binding, index):
    from workbench_platform.agent.runtime import AgentRuntime
    from scripts.evaluate_screen_agent_replay import copy_controller
    from packages.contracts.agent import AgentRef
    source_run = fpath(binding['native_run'])
    process = runtime = value = run = None
    start = time.perf_counter(); final_report = None
    try:
        if inventory(source_run) != binding['native_run_files']:
            raise ValueError('Original native evidence changed since preparation')
        if platform_binding(fpath(args.platform_source)) != binding['platform_source_files']:
            raise ValueError('Frozen Agent source/configuration changed since preparation')
        native = root / 'native/ocr'; native.parent.mkdir()
        shutil.copytree(source_run / 'native/ocr', native)
        if inventory(native) != inventory(source_run / 'native/ocr'):
            raise ValueError('Owned native copy differs from original files')
        byte_copy = cold_copy(source_run / 'native/ocr/workbench.sqlite3', native / 'workbench.sqlite3')
        logical_copy = verify_native_copy(native / 'workbench.sqlite3', index['native_table_identity_sha256'])
        frozen_json(root / 'native-copy-qualification.json', {'byte_copy': byte_copy, 'logical_copy': logical_copy})
        token_path = root / 'owned-reader-token.txt'; token_path.write_text(secrets.token_urlsafe(36), encoding='utf-8')
        token = token_path.read_text('utf-8')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        base = 'http://127.0.0.1:' + str(port)
        with (root / 'native-reader.log').open('wb') as log:
            process = subprocess.Popen(reader_command(args, root, binding, token_path, port), cwd=root,
                stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.perf_counter() + 60
            while time.perf_counter() < deadline:
                if process.poll() is not None:
                    raise ValueError('Owned disabled-queue reader exited; inspect its F: log')
                try:
                    state = await asyncio.to_thread(native_http, base, token, '/api/state')
                    break
                except (OSError, ValueError):
                    await asyncio.sleep(.2)
            else:
                raise ValueError('Owned disabled-queue reader did not become ready')
            if any((state.get(name) or {}).get('alive') for name in ('queue', 'fusion_queue', 'external_queue', 'document_queue')):
                raise ValueError('A forbidden native calculation queue was started')
            value, _, batch, fixed = platform_services(root, source_run, base, token, process, fpath(args.data_python))
            context = expose_owned_reader(value, batch, process, base, token, binding['bundle'])
            selected_row = next(row for row in index['rows'] if row['id'] == args.sample_id)
            members = [item for item in fixed.items if item.blob_hash == selected_row['input_sha256']]
            if len(members) != 1:
                raise ValueError('Existing native result has no unique original platform source')
            reference_asset = value.asset_library[0].register(batch['workspace_id'], 's07reference', 'reference.csv',
                media_type='text/csv', storage_mode='managed')
            runtime = AgentRuntime(root / 'platform/agent', value); value.agent = runtime
            missing_tools = (BUSINESS_TOOLS | DISCOVERY_TOOLS) - set(runtime.registry.tools)
            if missing_tools:
                raise ValueError('Required existing registered tool is absent: ' + ','.join(sorted(missing_tools)))
            runtime.registry.tools = {name: spec for name, spec in runtime.registry.tools.items()
                                      if name in BUSINESS_TOOLS | DISCOVERY_TOOLS}
            controller = copy_controller(runtime, fpath(args.controller_store))
            await runtime.start()
            reference = AgentRef(kind='asset', workspace_id=batch['workspace_id'], object_id=reference_asset.asset_id,
                                 revision=reference_asset.revision, sha256=reference_asset.blob_hash)
            session = runtime.store.create_session(batch['workspace_id'], 'S07真实原生结果核对与证据Agent',
                [members[0].asset_id, reference_asset.asset_id], 's07RealNativeAgentSession', [reference.model_dump()])
            goal = ('请用平台现有工具完成工程模拟核对并交付结果文件与直接审计证据。'
                '已选资料仅一张原图与reference.csv，范围已经明确。原图已有实际OCR结果，'
                f'关联link_id={context.link_id}，object_id={selected_row["result_id"]}，application=ocr。'
                '先通过原生结果回传核原图来源，再查询真实结构目录并将第0张表转成受控数据输入。'
                '将OCR表作为left，reference.csv作为right；第一行是表头，按Loci列严格匹配，仅比较Gene列原文，'
                'type_policy=strict、null_policy=never_match、trim_text=false、casefold_text=false、amount_pairs=[]、amount_tolerance=0。'
                '读取实际核对结果，固定直接审计证据并读回证据，导出数据结果并发布可下载成果。'
                '保留OCR原文与来源，差异只列证据，禁止修改值、补猜、扩展资料、重新OCR、训练、GPU调用或代签人工确认。'
                '仅用于英文工程样例，不推断科学或金额单位；含合并单元格或不完整结构则按实际工具限制说明，不能展开。')
            frozen_json(root / 'actual-goal.json', {'goal': goal, 'selected_assets': session['selected_assets'],
                'initial_refs': session['initial_refs'], 'controller': controller})
            agent_started = time.perf_counter()
            run = await runtime.submit(session['id'], goal, 's07RealNativeAgentGoal')
            deadline = agent_started + args.timeout; waiting = []
            while time.perf_counter() < deadline:
                current = runtime.store.run(run['id'])
                if current['status'] == 'waiting_job':
                    waiting.append({'elapsed_seconds': time.perf_counter() - agent_started,
                        'controller_requests': len(runtime.store.rows('SELECT id FROM model_requests WHERE run_id=?', (run['id'],)))})
                if current['status'] not in {'running', 'waiting_job'}:
                    break
                await asyncio.sleep(1)
            terminal = runtime.store.run(run['id'])
            agent_seconds = time.perf_counter() - agent_started
            expected_table = selected_row['edited_view']['tables'][args.table_index]
            missing, checks, files, tool_calls = validate_agent_result(runtime, value, run, root, expected_table, reference_asset)
            requests = runtime.store.rows('SELECT * FROM model_requests WHERE run_id=? ORDER BY created', (run['id'],))
            request_summary = [{key: row[key] for key in ('id', 'step', 'attempt', 'state', 'created', 'updated') if key in row} for row in requests]
            frozen_json(root / 'actual-controller-requests.json', request_summary)
            qualified = not missing and terminal['status'] in {'succeeded', 'partial', 'answered'} and bool(requests)
            final_report = {'format': 'specialized-real-agent-native-followup-result-v1',
                'status': 'real_agent_chain_verified' if qualified else 'real_agent_chain_incomplete',
                'agent_status': terminal['status'], 'run_id': run['id'], 'workspace_id': batch['workspace_id'],
                'prepared_sha256': digest(root / 'prepared.json'), 'native_sample_id': args.sample_id,
                'agent_orchestration_verified': qualified, 'actual_controller_requests': len(requests),
                'actual_tool_operations': tool_calls, 'missing_required_successful_tools': missing,
                'agent_wall_seconds': agent_seconds, 'followup_wall_seconds_before_shutdown': time.perf_counter() - start,
                'waiting_observations': waiting, 'native_inference_calls': 0, 'gpu_calls': 0, 'training_calls': 0,
                'human_signoff': False, 'literal_checks': checks, 'export_readback': files,
                'ancestor_direct_tool_qualification': str(Path(__file__).parents[2] / 'output/platform-specialized-agent-20261007/evaluation/s07-native-data-003-independent-001.json'),
                'scope': 'Real configured Agent and actual native adapters/registered tools on one isolated existing English OCR result plus explicitly simulated reference; no business accuracy or broad merged-table qualification'}
    except Exception as error:
        frozen_json(root / 's07-agent-native-failure.json', {'format': FORMAT, 'status': 'actual_agent_followup_failed',
            'error': str(error), 'run_id': run['id'] if run else None, 'native_inference_calls': 0, 'gpu_calls': 0})
        raise
    finally:
        try:
            if runtime:
                if run and runtime.store.run(run['id'])['status'] in {'running', 'waiting_job', 'waiting_user'}:
                    runtime.cancel(run['id'])
                await runtime.close()
        finally:
            try:
                if value:
                    try: value.batch_service.close()
                    finally: value.data_workbench.close()
            finally:
                if process and process.poll() is None:
                    process.terminate(); process.wait(timeout=10)
                unchanged = inventory(source_run) == binding['native_run_files']
                stopped = process is None or process.poll() is not None
                frozen_json(root / 'owned-read-service-stop.json', {'source_run_files_unchanged': unchanged,
                    'owned_process_exited': stopped, 'pid': process.pid if process else None, 'source_run': str(source_run)})
                if not unchanged or not stopped:
                    raise ValueError('Owned reader/source isolation cleanup did not pass')
    if platform_binding(fpath(args.platform_source)) != binding['platform_source_files']:
        raise ValueError('Frozen platform source changed during actual Agent execution')
    final_report['original_run_unchanged_and_reader_stopped'] = True
    final_report['stop_receipt_sha256'] = digest(root / 'owned-read-service-stop.json')
    frozen_json(root / 's07-agent-native-result.json', final_report)
    return final_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('native-run', 'inputs', 'truth', 'sample-id', 'platform-source', 'data-python',
                 'native-python', 'pdf-layer', 'output-root'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--table-index', type=int, default=0)
    parser.add_argument('--agent-layer', default='F:/OCR/output/platform-agent-dev-20261004/dependencies/agent')
    parser.add_argument('--controller-store', default='F:/OCR/output/ai-audit-product-dev-20261004/workspace/agent/operations.sqlite3')
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--run-controller', action='store_true')
    args = parser.parse_args()
    if args.table_index != 0 or args.sample_id != 'development-000-PMC4746643_table_3-normal':
        raise ValueError('This explicit Loci/Gene engineering task is qualified only for the named table')
    root = configure(args)
    binding, index = prepare(args, root)
    if args.run_controller:
        result = asyncio.run(execute(args, root, binding, index))
        print(result['status'], result['actual_controller_requests'], result['actual_tool_operations'])
    else:
        print('prepared', 'controller_calls=0', 'native_reader_started=false')


if __name__ == '__main__':
    main()
