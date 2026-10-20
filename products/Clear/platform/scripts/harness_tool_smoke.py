"""Sequential real-tool coverage in a fresh isolated local services composition.

No model calls, training, inference or live DATA. Native prepare is optional
and starts only the installed control services. This is not a claim that every
registered operation's full business workflow has been exercised.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.package_harness_candidate import DEFAULT_RELEASE, DEFAULT_WORK, environment, local_f, save, sha


def png():
    def chunk(name, payload):
        return struct.pack('>I', len(payload)) + name + payload + struct.pack('>I', zlib.crc32(name + payload))
    pixels = b''.join(b'\0' + bytes([40 + y, 100, 180] * 32) for y in range(32))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 32, 32, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


async def exercise(services, root, prepare_native):
    from packages.contracts.agent import AgentRef
    from workbench_platform.agent.registry import ToolContext, invoke
    from workbench_platform.application_services.job_lifecycle import adapter
    runtime = services.agent
    if runtime is None:
        raise ValueError('The isolated source Agent failed to initialize')
    registry = runtime.registry
    initial = {name: registry.describe(name, services) for name in registry.tools}
    if len(initial) != 106:
        raise ValueError('Unexpected registered tool count')
    workspace = services.workspace_store.create_workspace('Synthetic Harness tool coverage')
    workspace_id = workspace.workspace_id
    uploads = services.asset_library[2]
    fixtures = {'left.csv': b'id,supplier,amount\n000001, ACME ,10.00\n000002, OTHER ,20.00\n',
                'right.csv': b'id,supplier,amount\n000001, ACME ,11.00\n000002, OTHER ,20.00\n',
                'source.txt': b'Harness synthetic source evidence. No real business data.\n', 'image.png': png()}
    assets = {}
    for name, payload in fixtures.items():
        (uploads / name).write_bytes(payload)
        assets[name] = services.asset_library[0].register(workspace_id, 'uploads', name,
            media_type='image/png' if name.endswith('.png') else 'text/csv' if name.endswith('.csv') else 'text/plain', storage_mode='managed')
    session = runtime.store.create_session(workspace_id, 'Tool coverage', [value.asset_id for value in assets.values()], 'smokeSession')
    run, _ = runtime.store.start_run(session['id'], 'Validate existing tools using synthetic fixtures', 'smokeRun', 0)
    ctx = ToolContext(workspace_id, session['id'], run['id'], run['generation'], 0, '', tuple(session['selected_assets']), services, runtime.store)
    calls, serial = [], 0

    async def call(name, arguments=None, *, settle=True):
        nonlocal serial
        serial += 1
        call_index = serial
        command = {'call_id': 'smoke_' + str(serial), 'tool': name, 'arguments': arguments or {}}
        started = time.monotonic()
        try:
            registry.validate_batch([command], registry.tools)
            result = await registry.dispatch(command, ctx)
            pending = result if result['status'] == 'pending' else None
            if pending and settle:
                # Verify the registered job reader while the owned reference
                # still belongs to the pending operation's actual receipt.
                await call('jobs_get', {'job_id': pending['job_refs'][0]['object_id']})
                operation = runtime.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?', (run['id'], command['call_id']))[0]['id']
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    values = [await invoke(adapter(reference).settle, reference, ctx) for reference in pending['job_refs']]
                    if all(value is not None for value in values):
                        if len(values) != 1:
                            raise ValueError('Smoke expects one native job per fixture operation')
                        result = {**values[0], 'job_refs': pending['job_refs']}
                        runtime.store.finish_operation(operation, result)
                        break
                    await asyncio.sleep(.2)
                else:
                    raise TimeoutError('Native synthetic fixture timed out')
            entry = {'index': call_index, 'tool': name, 'arguments': command['arguments'], 'status': result['status'],
                'seconds': time.monotonic() - started, 'operation_id': runtime.store.rows('SELECT id FROM operations WHERE run_id=? AND call_id=?',
                    (run['id'], command['call_id']))[0]['id'], 'pending_receipt': pending, 'result': result}
        except Exception as error:
            result = {'status': 'script_error', 'summary': type(error).__name__, 'data': {}}
            entry = {'index': call_index, 'tool': name, 'arguments': command['arguments'], 'status': 'script_error',
                     'error_type': type(error).__name__, 'seconds': time.monotonic() - started}
        calls.append(entry)
        save(root / 'calls' / (str(len(calls)).zfill(3) + '-' + name + '.json'), entry)
        print(json.dumps({'tool': name, 'status': entry['status'], 'seconds': round(entry['seconds'], 2)}), flush=True)
        return result

    def source(name):
        value = assets[name]
        return AgentRef(kind='asset', workspace_id=workspace_id, object_id=value.asset_id,
                        revision=value.revision, sha256=value.blob_hash).model_dump()

    def require(value):
        if value['status'] != 'success':
            raise ValueError('A fixture prerequisite did not succeed; see call receipt')
        return value['data']

    (root / 'calls').mkdir()
    try:
        for name, arguments in [('workspace_get_context', {}), ('models_list', {}), ('tools_list', {'limit': 30}),
            ('tools_describe', {'names': ['assets_describe', 'documents_read', 'audit_write_finding', 'delivery_publish']}),
            ('assets_search', {}), ('assets_describe', {'asset_ids': [value.asset_id for value in assets.values()]}),
            ('artifacts_list', {}), ('audit_get_task', {}), ('audit_get_project', {}), ('audit_project_overview', {}),
            ('audit_formal_list', {}), ('audit_list_todos', {}), ('batch_list', {}), ('batch_list_templates', {}),
            ('session_read_history', {}), ('skills_list', {}), ('documents_read', {'reference': source('source.txt')}),
            ('results_describe', {'reference': source('source.txt')}), ('results_to_input', {'reference': source('source.txt')}),
            ('conversation_answer', {'content': 'Synthetic source and registered services are readable.'})]:
            await call(name, arguments)
        await call('assets_set_tags', {'asset_id': assets['left.csv'].asset_id, 'tags': ['synthetic-smoke']})
        task = runtime.tasks.get(session['id'])
        await call('audit_update_task', {'expected_revision': task['revision'], 'goal': 'Synthetic tool coverage; no model calls'})
        profile = runtime.tasks.profile(workspace_id)
        await call('audit_update_project', {'expected_revision': profile['revision'], 'audit_object': 'Synthetic fixture', 'rules': 'Preserve source identifiers'})
        await call('audit_update_plan', {'items': [{'key': 'read', 'title': 'Read synthetic sources', 'status': 'completed'}]})
        evidence = require(await call('audit_bind_evidence', {'reference': source('source.txt')}))
        await call('audit_read_evidence', {'evidence_id': evidence['evidence_id']})
        finding = await call('audit_write_finding', {'expected_revision': 0, 'title': 'Synthetic evidence saved',
            'facts': 'The source contains a synthetic evidence marker.', 'evidence_ids': [evidence['evidence_id']]})
        draft = require(finding)
        await call('audit_read_finding', {'draft_id': draft['draft_id']})
        await call('delivery_publish', {'summary': 'Saved fixed synthetic draft', 'result_refs': finding['result_refs']})
        paper = await call('audit_generate_workpaper', {'draft_ids': [draft['draft_id']], 'title': 'Synthetic workpaper'})
        if paper['status'] == 'success':
            await call('artifacts_get', {'bundle_id': paper['data']['bundle']['bundle_id']})
        await call('audit_search_documents', {'query': 'Harness'})

        data_session = require(await call('data_create_session', {'name': 'Synthetic comparison'}))['data_session_id']
        for side, filename in [('left', 'left.csv'), ('right', 'right.csv')]:
            asset = assets[filename]
            common = {'asset_id': asset.asset_id, 'asset_revision': asset.revision, 'blob_hash': asset.blob_hash}
            await call('data_inspect_input', common)
            require(await call('data_import', {**common, 'data_session_id': data_session,
                'expected_version': services.data_workbench.get(data_session)['version'], 'side': side}))
            require(await call('data_apply_step', {'data_session_id': data_session,
                'expected_version': services.data_workbench.get(data_session)['version'], 'side': side,
                'step': {'operator': 'trim', 'column': 'c2'}}))
            await call('data_read_table', {'data_session_id': data_session, 'side': side})
        compared = require(await call('data_compare', {'data_session_id': data_session,
            'expected_version': services.data_workbench.get(data_session)['version'],
            'spec': {'key_pairs': [['c1', 'c1']], 'compare_pairs': [['c2', 'c2'], ['c3', 'c3']]}}))
        result_id = services.data_workbench.get(data_session)['state']['result_id']
        await call('data_read_result', {'data_session_id': data_session, 'result_id': result_id})
        exported = await call('data_export', {'data_session_id': data_session,
            'expected_version': services.data_workbench.get(data_session)['version'], 'result_id': result_id})
        if exported['status'] == 'success':
            await call('artifacts_get', {'bundle_id': exported['data']['bundle']['bundle_id']})
            await call('delivery_publish', {'summary': 'Synthetic CSV comparison read back', 'artifact_refs': exported['artifact_refs']})
        template = await call('batch_template_from_session', {'session_id': data_session, 'name': 'Synthetic comparison template'})
        if template['status'] == 'success':
            configuration = template['data']
            await call('batch_create_template', {key: configuration[key] for key in ('name', 'scenario', 'config')})
            arguments = {'template_id': configuration['template_id'], 'template_version': configuration['version'],
                'inputs': [{'role': side, 'asset_id': assets[name].asset_id, 'asset_revision': assets[name].revision,
                            'blob_hash': assets[name].blob_hash} for side, name in [('left', 'left.csv'), ('right', 'right.csv')]]}
            await call('batch_preflight', arguments)
            batch = await call('batch_run', arguments)
            if batch['status'] == 'success':
                batch_id = batch['data']['batch']['batch_id']
                await call('batch_read', {'batch_id': batch_id})
                await call('batch_read_history', {'batch_id': batch_id})
        image = assets['image.png']
        inspected = await call('forensics_inspect', {'asset_id': image.asset_id, 'asset_revision': image.revision,
            'blob_hash': image.blob_hash, 'tools': ['metadata']})
        if inspected['status'] == 'success':
            inspection_id = inspected['data']['inspection_id']
            await call('forensics_read_report', {'inspection_id': inspection_id})
            await call('forensics_collect', {'inspection_id': inspection_id})

        project_fields = {'title': 'Synthetic formal project', 'audit_object': 'Synthetic fixtures',
                          'period_start': '2026-01-01', 'period_end': '2026-12-31', 'objective': 'Verify existing formal draft services'}
        formal = require(await call('audit_formal_create_project', {'project': project_fields}))
        project_id = formal['project_id']
        await call('audit_formal_read', {'project_id': project_id})
        await call('audit_formal_read_history', {'project_id': project_id})
        await call('audit_formal_update_project', {'project_id': project_id,
            'expected_revision': services.audit_service.get(project_id)['revision'], 'project': {**project_fields, 'objective': 'Read and verify fixed draft versions'}})
        procedure_fields = {'title': 'Synthetic procedure', 'basis': 'Synthetic source', 'scope': 'Synthetic assets only'}
        procedure = require(await call('audit_formal_create_procedure', {'project_id': project_id,
            'expected_revision': services.audit_service.get(project_id)['revision'], 'procedure': procedure_fields, 'batches': []}))
        procedure_id = procedure['entity_id']
        await call('audit_formal_update_procedure', {'project_id': project_id, 'entity_id': procedure_id,
            'expected_revision': services.audit_service.get(project_id)['revision'], 'procedure': {**procedure_fields, 'note': 'Read-back checked'}, 'batches': []})
        formal_finding = require(await call('audit_formal_create_finding', {'project_id': project_id, 'procedure_id': procedure_id,
            'expected_revision': services.audit_service.get(project_id)['revision'], 'finding': {'title': 'Synthetic finding'}, 'evidence': []}))
        await call('audit_formal_update_finding', {'project_id': project_id, 'entity_id': formal_finding['entity_id'],
            'expected_revision': services.audit_service.get(project_id)['revision'], 'finding': {'title': 'Synthetic finding', 'explanation': 'Local readback'}, 'evidence': []})
        todos = await call('audit_list_todos', {})
        if todos['status'] == 'success' and todos['data']['items']:
            await call('audit_locate_todo', {'todo_id': todos['data']['items'][0]['todo_id']})

        if prepare_native:
            native = {}
            for family in ('ocr', 'classification'):
                arguments = {'categories': ['synthetic-one', 'synthetic-two']} if family == 'classification' else {}
                prepared = await call(family + '_prepare', arguments)
                native[family] = prepared
                if prepared['status'] == 'success' and prepared['data'].get('link_id'):
                    link = prepared['data']['link_id']
                    await call('native_list_projects', {'application': family})
                    await call('native_list_results', {'application': family, 'link_id': link})
                    if family == 'ocr':
                        await call('ocr_list_processing_options', {'link_id': link})
                        await call('ocr_list_visual_models', {'link_id': link})
                    else:
                        fixed = require(await call('assets_create_set', {'asset_ids': [image.asset_id]}))
                        imported = await call('classification_import_assets', {'link_id': link,
                            'asset_set_id': fixed['asset_set_id'], 'asset_set_revision': fixed['revision']})
                        if imported['status'] == 'success':
                            handoff_id = imported['data']['handoff_id']
                            await call('classification_read_annotations', {'handoff_id': handoff_id})
            save(root / 'native-prepare.json', native)
            await call('models_list', {})
        skills = await call('skills_list', {})
        available = [item for item in skills.get('data', {}).get('items', []) if item.get('compatible')]
        if available:
            name = available[0]['name']
            await call('skills_activate', {'name': name})
            await call('skills_read_resource', {'name': name})
            await call('skills_deactivate', {'name': name})
    finally:
        final = {name: registry.describe(name, services)['availability'] for name in registry.tools}
        matrix = []
        historical = {'jobs_cancel_owned': 'tests/agent/test_recovery.py', 'jobs_retry_failed': 'tests/agent/test_native_tools.py',
            'batch_control': 'tests/agent/test_coverage_completion.py', 'batch_dispose': 'tests/agent/test_audit_product.py',
            'batch_export': 'tests/agent/test_audit_product.py', 'results_concat_tables': 'tests/agent/test_skills.py',
            'ask_user': 'tests/agent/test_recovery.py'}
        for name, metadata in initial.items():
            actual = [entry for entry in calls if entry['tool'] == name]
            successes = [entry for entry in actual if entry['status'] == 'success']
            matrix.append({'tool': name, 'effect': metadata['effect'], 'initial_availability': metadata['availability'],
                'final_availability': final[name], 'coverage': 'real_success' if successes else 'real_non_success' if actual else
                    'historical_test_reference' if name in historical else 'schema_and_readiness_only',
                'call_receipts': [str(entry['operation_id']) for entry in actual if entry.get('operation_id')],
                **({'historical_test_reference': historical[name], 'historical_reference_is_not_current_business_execution': True} if name in historical else {})})
        report = {'format': 'clear-harness-tool-smoke-v1', 'scope': 'Fresh synthetic local ASGI services; no real controller, training or inference',
            'workspace_id': workspace_id, 'session_id': session['id'], 'run_id': run['id'], 'registered': len(initial),
            'model_requests': len(runtime.store.rows('SELECT id FROM model_requests')), 'prepare_native_requested': prepare_native,
            'real_success_tools': sum(row['coverage'] == 'real_success' for row in matrix),
            'real_non_success_tools': sum(row['coverage'] == 'real_non_success' for row in matrix),
            'schema_only_tools': sum(row['coverage'] == 'schema_and_readiness_only' for row in matrix),
            'historical_reference_tools': sum(row['coverage'] == 'historical_test_reference' for row in matrix),
            'readiness_does_not_prove_model_availability': True, 'all_registered_tools_business_executed': False,
            'matrix': matrix, 'calls': [{'tool': row['tool'], 'status': row['status'], 'operation_id': row.get('operation_id')} for row in calls]}
        save(root / 'coverage.json', report)
        with runtime.store.transaction() as db:
            runtime.store.cancel_in_transaction(db, run_id=run['id'])
        print(json.dumps({key: value for key, value in report.items() if key not in {'matrix', 'calls'}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sealed-release', type=Path, default=DEFAULT_RELEASE)
    parser.add_argument('--work', type=Path, default=DEFAULT_WORK)
    parser.add_argument('--prepare-native', action='store_true')
    args = parser.parse_args()
    root, sealed, work = local_f(args.output), local_f(args.sealed_release, exists=True), local_f(args.work, exists=True)
    if root.exists() or not root.is_relative_to(work):
        raise ValueError('Use a fresh tool-smoke output beneath the F task root')
    environment(work)
    root.mkdir(parents=True)
    data = root / 'data'
    source = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(source), str(source / 'src')]
    os.environ.update(WORKBENCH_AGENT_LAYER=str(sealed / 'dependencies/agent'), WORKBENCH_AGENT_ENABLED='1',
        WORKBENCH_PORTAL_DIST=str(source / 'apps/portal/dist'), CLEAR_PADDLE_TEMP=str(root / 'temp'),
        CUDA_VISIBLE_DEVICES='', PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True', LANGCHAIN_TRACING_V2='false', LANGSMITH_TRACING='false')
    from workbench_platform.supervisor.processes import storage_environment
    os.environ.update(storage_environment(data))
    os.environ['TMPDIR'] = os.environ['TEMP']
    from scripts.harness_io_guard import install_guard
    install_guard(write_roots=(root,), read_roots=(source, sealed))
    from scripts.agent_native_preview import configure
    configure(sealed / 'platform', sealed / 'program', data)
    from fastapi.testclient import TestClient
    from workbench_platform.app import app
    with TestClient(app) as client:
        client.portal.call(exercise, app.state, root, args.prepare_native)


if __name__ == '__main__':
    main()
