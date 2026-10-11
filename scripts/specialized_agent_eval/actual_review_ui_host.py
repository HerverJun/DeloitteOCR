"""Serve an owned byte copy of a stopped actual batch for UI review admission.

Review actions are scripted engineering operations, never human quality or
review-relief evidence. No queue, coordinator, model or Agent is started.
"""
import argparse
import json
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time

from common import digest, fpath, frozen_json, read_json
from controlled_recovery import require_environment
from cold_backup_restore_native import checkpoint_owned, inventory, require
from freeze_sources import freeze
from precision_route_cold_restore import execution_ledger


def immutable_execution(ledger):
    return {
        'batches': [{k: v for k, v in row.items() if k != 'status'} for row in ledger['batches']],
        'items': [{k: v for k, v in row.items() if k not in
                   {'status', 'stage', 'result_id', 'result_revision', 'review_state', 'error'}}
                  for row in ledger['items']],
        **{key: ledger[key] for key in ('tasks', 'model_costs', 'context_costs')},
    }


def verify_stopped(output, host_exit_error=None):
    """Hash protected originals outside the consumer's old-root deny hook."""
    output = fpath(output)
    ready, scope = read_json(output / 'ready.json'), read_json(output / 'scope.json')
    from urllib.parse import urlsplit
    with socket.socket() as probe:
        probe.settimeout(2)
        require(probe.connect_ex(('127.0.0.1', urlsplit(ready['url']).port)) != 0,
                'Review host still accepts connections')
    before = read_json(output / 'initial-execution-ledger.json')
    after = execution_ledger(output / 'runtime/ocr')
    frozen_json(output / 'final-execution-ledger.json', after)
    protected = immutable_execution(after) == immutable_execution(before)
    unchanged = inventory(fpath(scope['source_run']) / 'native/ocr') == read_json(output / 'original-native-bytes-before.json')
    require(unchanged and protected, 'Review changed protected source/execution history')
    frozen_json(output / 'stopped.json', {'servers_stopped': True, 'loopback_connection_refused': True,
        'queues_started': False, 'new_model_calls': 0, 'new_agent_calls': 0,
        'original_native_bytes_unchanged': unchanged,
        'execution_identity_budget_cost_and_failures_preserved': protected,
        'verification_process_separate_from_consumer': True,
        'host_exit_error': host_exit_error})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-run', 'output', 'worktrees', 'bundle', 'pdf-layer'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--port', type=int, default=8792)
    args = parser.parse_args()
    require_environment()
    output, source_run = fpath(args.output), fpath(args.source_run)
    result = read_json(source_run / 'result.json')
    require(result['status'] == 'terminal' and result['counts']['pending'] == 0,
            'Source must be the complete stopped actual batch')
    require(not output.exists(), 'Use a new immutable UI review identity')
    output.mkdir(parents=True)
    freeze(args.worktrees, output / 'source')
    original, owned = source_run / 'native/ocr', output / 'runtime/ocr'
    before = inventory(original)
    frozen_json(output / 'original-native-bytes-before.json', before)
    owned.parent.mkdir(parents=True)
    shutil.copytree(original, owned)
    require(inventory(owned) == before == inventory(original), 'Actual byte copy changed')
    checkpoint_owned(owned)
    ledger = execution_ledger(owned)
    frozen_json(output / 'initial-execution-ledger.json', ledger)
    sys.path[:0] = [str(output / 'source/platform'), str(output / 'source/platform/src'),
                    str(output / 'source/ocr/src'), str(fpath(args.pdf_layer).parent)]
    from scripts.backup_workspace import _relocate_screen_paths
    relocation = _relocate_screen_paths(owned, owned, {
        'kind': 'ocr', 'source_data_dir': str(original),
        'files': {name: {'sha256': value, 'sqlite': name.endswith('.sqlite3')}
                  for name, value in inventory(owned).items() if not name.endswith(('-wal', '-shm'))},
    })
    # Deny accidental production/earlier-evidence data reads after the copy.
    from cold_backup_restore_native import install_native_read_audit
    install_native_read_audit(output / 'native-file-reads.jsonl', owned, [original])
    import uvicorn
    from ocr_workbench.service import create_app
    token = secrets.token_hex(32)
    app = create_app(fpath(args.bundle), owned, token, start_queue=False,
                     agent_enabled=False, web_root=output / 'source/ocr/frontend/dist')
    project, batch = ledger['batches'][0]['project_id'], result['batch_id']
    details = [app.state.screen_batches.detail(project, batch, row['input_version_id'])
               for row in ledger['items']]
    require(len(details) == result['scope_inputs'], 'Actual full scope changed')
    # Pick actual existing two-reading metadata; no injected candidate/context.
    choices = []
    for position, detail in enumerate(details):
        for index, part in enumerate(detail.get('business_metadata', [])):
            evidence = part.get('transcription_evidence') or {}
            if (evidence.get('primary_source') and 'primary_text' in evidence
                    and 'auxiliary_text' in evidence and detail['result']['edited']['tables']):
                choices.append((position, index, detail))
    require(choices, 'Actual batch contains no two-reading metadata to exercise')
    position, metadata_index, selected = choices[0]
    scope = {'format': 'actual-review-ui-scope-v1', 'source_run': str(source_run),
        'source_result_sha256': digest(source_run / 'result.json'),
        'project_id': project, 'batch_id': batch, 'images': len(details),
        'selected_position': position, 'selected_version_id': selected['input_version_id'],
        'selected_source_name': selected['source_name'], 'metadata_index': metadata_index,
        'new_model_calls': 0, 'new_agent_calls': 0, 'queues_started': False,
        'scripted_review_actions': True, 'human_quality_or_relief_qualification': False,
        'initial_owned_path_relocation': relocation}
    frozen_json(output / 'scope.json', scope)
    frozen_json(output / 'initial-details.json', details)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 60
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.1)
        require(server.started, 'Owned no-queue review host did not start')
        frozen_json(output / 'ready.json', {'url': f'http://127.0.0.1:{args.port}',
            'token': token, 'project_id': project, 'batch_id': batch})
        print(json.dumps({'ready': True, 'images': len(details), 'output': str(output)}), flush=True)
        while not (output / 'stop.request').exists() and thread.is_alive():
            time.sleep(.2)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        after = execution_ledger(owned)
        frozen_json(output / 'final-execution-ledger.json', after)
        protected = immutable_execution(after) == immutable_execution(ledger)
        require(not thread.is_alive() and protected, 'Review host failed to stop or changed execution history')
        # A Python audit hook cannot be removed. The consumer must keep denying
        # the old source, while a separate verifier hashes it as opaque bytes.
        subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c',
            "import sys;sys.path.insert(0,sys.argv[1]);from actual_review_ui_host import verify_stopped;verify_stopped(sys.argv[2])",
            str(fpath(__file__).parent), str(output)], cwd=output, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--verify-stopped':
        verify_stopped(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        main()
