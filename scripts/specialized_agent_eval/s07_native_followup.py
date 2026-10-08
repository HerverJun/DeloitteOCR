"""Actual registered CPU data/evidence tools; native handoff requires real services.

The standalone file example is deliberately labelled file->data->evidence, never
as proof of OCR native identity or Agent/controller orchestration. The shared
run_tools function also accepts genuine results_to_input assets in a live scope.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import copy
from dataclasses import replace
import json
import sys
import time
from types import SimpleNamespace
from common import FORMAT, digest, fingerprint, fpath, frozen_bytes, frozen_json


async def dispatch(registry, ctx, name, arguments, traces, *, call_prefix='s07DataCall'):
    call = {'call_id': call_prefix + str(len(traces) + 1), 'tool': name, 'arguments': arguments}
    started = time.perf_counter()
    result = await registry.dispatch(call, ctx)
    trace = {'call': call, 'initial_receipt': result, 'wall_seconds': time.perf_counter() - started}
    traces.append(trace)
    if result['status'] == 'pending':
        from workbench_platform.agent.tools.data import settle
        job = result['job_refs'][0]
        deadline = time.perf_counter() + 190
        while time.perf_counter() < deadline:
            completed = await asyncio.to_thread(settle, job, ctx)
            if completed is not None:
                result = completed
                break
            await asyncio.sleep(.1)
        else:
            raise ValueError('Actual CPU data job did not return a terminal receipt')
        trace.update(terminal_receipt=result, wall_seconds=time.perf_counter() - started)
        operations = ctx.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?', (ctx.run_id, call['call_id']))
        if len(operations) != 1:
            raise ValueError('CPU tool terminal receipt has no unique actual operation')
        ctx.store.finish_operation(operations[0]['id'], result)
    if result['status'] != 'success':
        raise ValueError('Actual registered ' + name + ' tool returned ' + result['status'])
    return result


def column_id(value, header):
    matches = [column['column_id'] for column in value['data']['metadata']['columns'] if column['header'] == header]
    if len(matches) != 1:
        raise ValueError('Explicit business header is absent/ambiguous in actual imported columns: ' + header)
    return matches[0]


async def run_tools(registry, ctx, left, right, *, key_header, compare_headers, amount_headers,
                    rule, output, origin_scope):
    output = fpath(output)
    traces = []
    readbacks = {}
    before_controller = len(ctx.store.rows('SELECT id FROM model_requests WHERE run_id=?', (ctx.run_id,)))
    created = await dispatch(registry, ctx, 'data_create_session', {'name': 'S07实际工具核对'}, traces)
    session_id = created['data']['data_session_id']
    imported = {}
    for side, asset in (('left', left), ('right', right)):
        args = {key: asset[key] for key in ('asset_id', 'asset_revision', 'blob_hash')}
        inspected = await dispatch(registry, ctx, 'data_inspect_input', args, traces)
        if inspected['data'].get('requires_sheet_selection'):
            raise ValueError('Actual input has multiple sheets; explicit sheet selection is required')
        version = ctx.services.data_workbench.get(session_id)['version']
        await dispatch(registry, ctx, 'data_import', {**args, 'data_session_id': session_id,
            'expected_version': version, 'side': side}, traces)
        imported[side] = await dispatch(registry, ctx, 'data_read_table',
            {'data_session_id': session_id, 'side': side, 'offset': 0, 'limit': 50}, traces)
    keys = [[column_id(imported['left'], key_header), column_id(imported['right'], key_header)]]
    pairs = [[column_id(imported['left'], header), column_id(imported['right'], header)] for header in compare_headers]
    amounts = [[column_id(imported['left'], header), column_id(imported['right'], header)] for header in amount_headers]
    if any(pair not in pairs for pair in amounts):
        raise ValueError('Explicit amount columns must also be explicit comparison columns')
    spec = {'key_pairs': keys, 'compare_pairs': pairs, 'amount_pairs': amounts, 'type_policy': 'strict',
            'null_policy': 'never_match', 'amount_tolerance': '0', 'trim_text': False, 'casefold_text': False}
    version = ctx.services.data_workbench.get(session_id)['version']
    compared = await dispatch(registry, ctx, 'data_compare',
        {'data_session_id': session_id, 'expected_version': version, 'spec': spec}, traces)
    result_id = compared['data']['result_id']
    result = await dispatch(registry, ctx, 'data_read_result',
        {'data_session_id': session_id, 'result_id': result_id, 'offset': 0, 'limit': 50}, traces)
    evidence = await dispatch(registry, ctx, 'audit_bind_evidence',
        {'reference': result['result_refs'][0], 'offset': 0, 'limit': 20}, traces)
    reread = await dispatch(registry, ctx, 'audit_read_evidence',
        {'evidence_id': evidence['data']['evidence_id']}, traces)
    if (reread['data']['snapshot_hash'] != evidence['data']['snapshot_hash'] or reread['data'].get('stale')):
        raise ValueError('Actual data evidence snapshot changed or is unexpectedly stale')
    version = ctx.services.data_workbench.get(session_id)['version']
    exported = await dispatch(registry, ctx, 'data_export',
        {'data_session_id': session_id, 'expected_version': version, 'result_id': result_id}, traces)
    bundle = exported['data']['bundle']
    for entry in bundle['files']:
        artifact = ctx.services.artifact_center.file(ctx.workspace_id, bundle['bundle_id'], entry['name'])
        with ctx.services.artifact_center.store.stage_download(ctx.workspace_id, artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
            sha = digest(path)
            if sha != artifact.blob_hash:
                raise ValueError('Actual data export bytes differ from artifact record')
            target = output / 'delivered' / entry['name']
            frozen_bytes(target, path.read_bytes())
        readbacks[entry['name']] = {'path': str(target), 'sha256': sha, 'artifact_id': artifact.artifact_id}
    if len(ctx.store.rows('SELECT id FROM model_requests WHERE run_id=?', (ctx.run_id,))) != before_controller:
        raise ValueError('Deterministic CPU followup unexpectedly invoked a controller')
    report = {'format': 'specialized-existing-data-evidence-result-v1', 'status': 'actual_registered_tools_completed',
        'scope': origin_scope, 'workspace_id': ctx.workspace_id, 'session_id': ctx.session_id, 'run_id': ctx.run_id,
        'inputs': {'left': left, 'right': right}, 'explicit_business_rule': rule, 'actual_compare_spec': spec,
        'data_result': result, 'evidence': reread, 'data_export': bundle, 'export_readback': readbacks,
        'traces': traces, 'controller_calls': 0, 'model_calls': 0, 'training_calls': 0,
        'human_signoff': False, 'ocr_native_identity_verified': origin_scope.startswith('Actual native OCR'),
        'agent_orchestration_verified': False,
        'limits': ['A deterministic evaluation runner selected these registered tools; this does not prove controller/skill planning.',
                   'Comparison preserves original values and records differences; no OCR value is corrected or human-accepted.',
                   'Audit evidence fixes a declared first-20-row window with versioned source assets; the complete result remains separately readable.']}
    frozen_json(output / 's07-result.json', report)
    return report


def services_for_files(root, data_python):
    from fastapi import FastAPI
    from workbench_platform.store import WorkspaceStore
    from workbench_platform.assets.registry import AssetRegistry
    from workbench_platform.assets.sets import AssetSetStore
    from workbench_platform.artifacts.center import ArtifactCenter
    from workbench_platform.data_api.service import DataWorkbenchService
    from workbench_platform.batches.service import BatchService
    from workbench_platform.audit.service import AuditService
    from workbench_platform.agent.store import AgentStore
    from workbench_platform.agent.tasks import AuditTasks
    root = fpath(root); root.mkdir(parents=True, exist_ok=True)
    (root / 'blobs').mkdir(exist_ok=True); (root / 'uploads').mkdir(exist_ok=True)
    workspaces = WorkspaceStore(root / 'workspaces.sqlite3')
    if workspaces.get_workspace('s07workspace') is None:
        workspaces.create_workspace('S07隔离真实工具样例', workspace_id='s07workspace')
    registry = AssetRegistry(root / 'assets.sqlite3', root / 'blobs', {'inputs': root / 'inputs', 'uploads': root / 'uploads'})
    sets = AssetSetStore(registry)
    data = DataWorkbenchService(root / 'data', fpath(data_python)); data.asset_registry = registry
    center = ArtifactCenter(root / 'artifacts', registry, sets, data)
    value = SimpleNamespace(workspace_store=workspaces, asset_library=(registry, sets, None),
        data_workbench=data, artifact_center=center)
    app = FastAPI(); app.state = value
    value.batch_service = BatchService(app, root / 'batches'); value.batch_store = value.batch_service.store
    value.audit_service = AuditService(app, root / 'audit')
    store = AgentStore(root / 'agent')
    value.agent = SimpleNamespace(store=store, services=value)
    value.agent.tasks = AuditTasks(value.agent)
    return value, store


async def files_example(args):
    root, platform_source = fpath(args.output_root), fpath(args.platform_source)
    sys.path[:0] = [str(platform_source), str(platform_source / 'src')]
    from workbench_platform.agent.registry import ToolContext, ToolRegistry
    from workbench_platform.agent.tools import data, audit
    original_hashes, registered = {}, {}
    for side, original in (('left', fpath(args.left)), ('right', fpath(args.right))):
        if original.suffix.lower() not in {'.csv', '.xlsx'}:
            raise ValueError('S07 actual file input must be explicit CSV or XLSX')
        sha = digest(original); original_hashes[str(original)] = sha
        target = root / 'inputs' / (side + original.suffix.lower())
        frozen_bytes(target, original.read_bytes()); registered[side] = target
    value, store = services_for_files(root, args.data_python)
    run = None
    try:
        assets = {}
        for side, path in registered.items():
            asset = value.asset_library[0].register('s07workspace', 'inputs', path.name,
                media_type='text/csv' if path.suffix.lower() == '.csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                storage_mode='managed')
            assets[side] = {'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash}
        session = store.create_session('s07workspace', 'S07实际数据核对与直接证据',
            [entry['asset_id'] for entry in assets.values()], 's07DataSession')
        run, _ = store.start_run(session['id'], args.rule, 's07DataRun', 0)
        value.agent.tasks.record_goal(session['id'], args.rule)
        ctx = ToolContext('s07workspace', session['id'], run['id'], run['generation'], 0, '',
                          tuple(session['selected_assets']), value, store)
        registry = ToolRegistry(); data.register(registry); audit.register(registry)
        report = await run_tools(registry, ctx, assets['left'], assets['right'], key_header=args.key_header,
            compare_headers=args.compare_header, amount_headers=args.amount_header, rule=args.rule, output=root,
            origin_scope='Actual files -> existing registered CPU data tools -> direct audit evidence; OCR/native/model/Agent planning not verified')
        if any(digest(path) != sha for path, sha in original_hashes.items()):
            raise ValueError('Read-only input source changed during CPU registered-tool execution')
        store.status(run['id'], run['generation'], 'completed')
        return report
    except Exception as error:
        if run is not None:
            store.status(run['id'], run['generation'], 'failed', str(error))
        frozen_json(root / 's07-failure.json', {'format': FORMAT, 'status': 'actual_tool_chain_failed',
            'error': str(error), 'scope': 'Actual file-to-data-to-evidence execution; partial actual records retained',
            'model_calls': 0, 'controller_calls': 0})
        raise
    finally:
        value.batch_service.close(); value.data_workbench.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform-source', required=True)
    parser.add_argument('--data-python', required=True)
    parser.add_argument('--left', required=True); parser.add_argument('--right', required=True)
    parser.add_argument('--key-header', required=True)
    parser.add_argument('--compare-header', action='append', required=True)
    parser.add_argument('--amount-header', action='append', default=[])
    parser.add_argument('--rule', required=True)
    parser.add_argument('--output-root', required=True)
    result = asyncio.run(files_example(parser.parse_args()))
    print(result['status'], result['scope'])
