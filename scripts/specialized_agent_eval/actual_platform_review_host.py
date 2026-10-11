"""Full production Portal and HTTP bridge over an owned actual 40-image copy.

Only trusted native launch/context callbacks are bound to the isolated service.
No recognition queue, coordinator, model or Agent is started; OCR is not seeded.
"""
import argparse
from contextlib import asynccontextmanager
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import require_environment
from cold_backup_restore_native import checkpoint_owned, inventory, require, install_native_read_audit
from freeze_sources import freeze
from precision_route_cold_restore import execution_ledger
from actual_review_ui_host import immutable_execution


COPY_MAP = {'native/ocr': 'ocr', 'workspaces.sqlite3': 'platform/workspaces.sqlite3',
    'assets.sqlite3': 'platform/assets.sqlite3', 'blobs': 'platform/asset-blobs',
    'inputs': 'platform/asset-uploads/replay', 'ocr-handoff.sqlite3': 'platform/ocr-handoffs.sqlite3',
    'screen-batches.sqlite3': 'platform/screen-batches.sqlite3', 'artifacts': 'platform/artifact-center'}


def old_scope(ledger, initial):
    ids = {row['id'] for row in initial['batches']}
    return {key: [row for row in rows if row.get('batch_id', row.get('id')) in ids]
            if key in {'batches', 'items'} else rows for key, rows in ledger.items()}


def verify_stopped(output):
    output = fpath(output)
    ready, scope = read_json(output / 'ready.json'), read_json(output / 'scope.json')
    from urllib.parse import urlsplit
    for key in ('native', 'platform'):
        with socket.socket() as probe:
            probe.settimeout(2)
            require(probe.connect_ex(('127.0.0.1', urlsplit(ready[key]).port)) != 0, 'Host still accepts connections')
    initial = read_json(output / 'initial-execution-ledger.json')
    final = execution_ledger(output / 'runtime/ocr')
    frozen_json(output / 'final-execution-ledger.json', final)
    require(immutable_execution(old_scope(final, initial)) == immutable_execution(initial), 'Original execution history changed')
    original = fpath(scope['source_run'])
    for name, before in read_json(output / 'original-bytes-before.json').items():
        source = original / name
        require((inventory(source) if source.is_dir() else digest(source)) == before, 'Original source changed: ' + name)
    require(final['tasks'] == initial['tasks'], 'Unexpected model/queue work')
    frozen_json(output / 'stopped.json', {'servers_stopped': True, 'loopback_connections_refused': True,
        'queues_started': False, 'coordinator_started': False, 'model_calls': 0, 'agent_calls': 0,
        'original_source_bytes_unchanged': True, 'original_execution_history_preserved': True,
        'additional_submission_batches': len(final['batches']) - len(initial['batches'])})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-run', 'output', 'worktrees', 'bundle', 'pdf-layer'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--native-port', type=int, default=8794)
    parser.add_argument('--platform-port', type=int, default=8793)
    args = parser.parse_args()
    require_environment()
    source, output = fpath(args.source_run), fpath(args.output)
    result = read_json(source / 'result.json')
    result_sha = digest(source / 'result.json')
    require(result['status'] == 'terminal' and result['scope_inputs'] == 40, 'Use complete actual validation run')
    require(not output.exists(), 'Use a new isolated identity')
    output.mkdir(parents=True)
    frozen_json(output / 'source-result.json', result)
    freeze(args.worktrees, output / 'source')
    before = {}
    for name, destination in COPY_MAP.items():
        original, target = source / name, output / 'runtime' / destination
        before[name] = inventory(original) if original.is_dir() else digest(original)
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_dir():
            shutil.copytree(original, target)
        else:
            shutil.copyfile(original, target)
            for suffix in ('-wal', '-shm'):
                sidecar = original.with_name(original.name + suffix)
                if sidecar.exists():
                    before[name + suffix] = digest(sidecar)
                    shutil.copyfile(sidecar, target.with_name(target.name + suffix))
        require((inventory(target) if target.is_dir() else digest(target)) == before[name], 'Copy differs')
    frozen_json(output / 'original-bytes-before.json', before)
    owned = output / 'runtime/ocr'
    checkpoint_owned(owned)
    initial = execution_ledger(owned)
    frozen_json(output / 'initial-execution-ledger.json', initial)
    sys.path[:0] = [str(output / 'source/platform'), str(output / 'source/platform/src'),
                    str(output / 'source/ocr/src'), str(fpath(args.pdf_layer).parent)]
    from scripts.backup_workspace import _relocate_screen_paths
    relocation = _relocate_screen_paths(owned, owned, {'kind': 'ocr', 'source_data_dir': str(source / 'native/ocr'),
        'files': {name: {'sha256': value, 'sqlite': name.endswith('.sqlite3')}
                  for name, value in inventory(owned).items() if not name.endswith(('-wal', '-shm'))}})
    install_native_read_audit(output / 'file-reads.jsonl', output / 'runtime', [source])
    os.environ['WORKBENCH_DATA_DIR'] = str(output / 'runtime/platform')
    os.environ['WORKBENCH_AGENT_ENABLED'] = '0'
    os.environ['WORKBENCH_PORTAL_DIST'] = str(output / 'source/platform/apps/portal/dist')
    import uvicorn
    from ocr_workbench.service import create_app
    native_token = secrets.token_hex(32)
    native = create_app(fpath(args.bundle), owned, native_token, start_queue=False,
                        agent_enabled=False, web_root=output / 'source/ocr/frontend/dist')
    project, batch = initial['batches'][0]['project_id'], result['batch_id']
    details = [native.state.screen_batches.detail(project, batch, row['input_version_id']) for row in initial['items']]
    selected = next((position, index, detail) for position, detail in enumerate(details)
        for index, part in enumerate(detail.get('business_metadata', []))
        if part.get('transcription_evidence', {}).get('primary_source')
        and 'auxiliary_text' in part.get('transcription_evidence', {}) and detail['result']['edited']['tables'])
    position, metadata_index, detail = selected
    frozen_json(output / 'initial-details.json', details)
    import workbench_platform.app as module
    from workbench_platform.bridge.context import ResolvedContext
    module.configure_forensics = lambda app: setattr(app.state, 'forensic_service', None)
    original_lifespan = module.lifespan
    native_url, platform_url = f'http://127.0.0.1:{args.native_port}', f'http://127.0.0.1:{args.platform_port}'

    @asynccontextmanager
    async def isolated_lifespan(app):
        async with original_lifespan(app):
            registry, sets, uploads = app.state.asset_library
            registry.roots['replay'] = fpath(uploads / 'replay')
            links = [link for link in app.state.workspace_store.list_links('wreplay')
                     if link.app_id == 'ocr' and link.module_project_id == project]
            require(len(links) == 1, 'Copied project link changed')
            link = links[0]
            context = ResolvedContext('wreplay', 'ocr', 'replayInstance', link.link_id, project,
                                     link.role, '/apps/ocr/workflow', native_url)
            jobs = app.state.job_service
            def bound(workspace, family, link_id, instance_id=None):
                require((workspace, family, link_id) == ('wreplay', 'ocr', link.link_id)
                        and instance_id in (None, context.instance_id), 'Outside isolated trusted context')
                return context
            jobs._context, jobs._token = bound, lambda: native_token
            jobs._live = lambda supplied: supplied == context
            old_current = jobs.launches.current
            jobs.launches.current = lambda family: (SimpleNamespace(instance_id=context.instance_id, base_url=native_url)
                                                    if family == 'ocr' else old_current(family))
            fixed = sets.get('wreplay', 'setReplay', 1)
            require(len(fixed.items) == 40, 'Copied fixed full input set differs')
            app.state.screen_batches.status('wreplay', batch)
            frozen_json(output / 'ready.json', {'native': native_url, 'platform': platform_url,
                'token': native_token, 'project_id': project, 'batch_id': batch, 'workspace_id': 'wreplay',
                'link_id': link.link_id, 'asset_set_id': 'setReplay', 'asset_set_revision': fixed.revision})
            yield
    module.app.router.lifespan_context = isolated_lifespan
    frozen_json(output / 'scope.json', {'format': 'actual-platform-review-scope-v1', 'source_run': str(source),
        'source_result_sha256': result_sha, 'project_id': project, 'batch_id': batch,
        'images': len(details), 'selected_position': position, 'selected_version_id': detail['input_version_id'],
        'selected_source_name': detail['source_name'], 'metadata_index': metadata_index,
        'trusted_native_launch_and_context_callbacks_bound': True, 'full_production_portal': True,
        'ocr_results_injected': False, 'queues_started': False, 'model_calls': 0, 'agent_calls': 0,
        'human_quality_or_relief_qualification': False, 'initial_owned_path_relocation': relocation})
    servers, threads = [], []
    try:
        for app, port in ((native, args.native_port), (module.app, args.platform_port)):
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
            thread = threading.Thread(target=server.run, daemon=True)
            servers.append(server); threads.append(thread); thread.start()
            deadline = time.monotonic() + 90
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.1)
            require(server.started, 'Owned host failed to start')
        print(json.dumps({'ready': True, 'images': len(details), 'output': str(output)}), flush=True)
        while not (output / 'stop.request').exists() and all(thread.is_alive() for thread in threads):
            time.sleep(.2)
    finally:
        for server in reversed(servers): server.should_exit = True
        for thread in reversed(threads): thread.join(timeout=15)
        require(all(not thread.is_alive() for thread in threads), 'Owned server did not stop')
        subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c',
            'import sys;sys.path.insert(0,sys.argv[1]);from actual_platform_review_host import verify_stopped;verify_stopped(sys.argv[2])',
            str(fpath(__file__).parent), str(output)], cwd=output, check=True, creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == '__main__': main()
