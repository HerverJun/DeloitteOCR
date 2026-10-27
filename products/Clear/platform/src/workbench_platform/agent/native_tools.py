"""Journal and authorize official DSH tools without replacing their executor."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

from .general import enabled
from .registry import tool_activity
from .store import canonical, digest, Conflict
from workbench_platform.supervisor.processes import require_f_path

READ_TOOLS = {'read', 'read_image', 'glob', 'grep', 'web_fetch', 'web_search', 'task_list',
              'task_read', 'task_output', 'wait_task', 'job_list', 'job_output', 'list_agents', 'skill'}


def workspace(runtime, state):
    root = require_f_path(runtime.store.root / 'workspaces' / state['session_id'])
    root.mkdir(parents=True, exist_ok=True)
    state['working_directory'] = str(root)
    manifest = []
    registry = runtime.services.asset_library[0]
    for asset_id in runtime.store.session(state['session_id'])['selected_assets']:
        asset = registry.get(state['workspace_id'], asset_id)
        basename = Path(asset.original_name).name
        name = ''.join(c if c.isalnum() or c in '._- ' else '_' for c in basename)
        target = require_f_path(root / 'inputs' / asset_id / (name or 'input'))
        target.parent.mkdir(parents=True, exist_ok=True)
        current = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if current != asset.blob_hash:
            # A changed working copy is user work. Preserve it instead of
            # silently restoring input bytes into an edited file.
            if current is not None:
                target = require_f_path(target.with_name(target.stem + '-' + asset.blob_hash[:12] + target.suffix))
            with registry.stage_verified(state['workspace_id'], asset_id, revision=asset.revision,
                                         blob_hash=asset.blob_hash) as original:
                if not target.exists():
                    shutil.copyfile(original, target)
        manifest.append({'asset_id': asset_id, 'revision': asset.revision, 'sha256': asset.blob_hash,
                         'name': asset.original_name, 'path': str(target)})
    manifest_path = require_f_path(root / '.clear' / 'inputs.json')
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(canonical(manifest), encoding='utf-8')
    state['workspace_inputs'] = manifest
    if 'workspace_baseline' not in state:
        from .workspace_outputs import output_files
        state['workspace_baseline'] = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                       for path in output_files(root)}
    return root


def environment(runtime, state, env):
    root = workspace(runtime, state)
    from .windows_workspace import prepare_owner_access
    ancestors = {root.parent, runtime.store.root, runtime.store.root.parent,
                 Path(env['TEMP']).parent}
    state['execution_boundary'] = {'mechanism': 'dsh-windows-restricted-token',
        'owner_access': [*[prepare_owner_access(path, writable=False, inherit=False)
                           for path in sorted(ancestors)],
                         prepare_owner_access(root), prepare_owner_access(Path(env['TEMP']))]}
    private = root / '.clear'
    for name in ('appdata', 'localappdata', 'cache', 'config', 'data', 'npm', 'pip', 'python', 'huggingface', 'torch'):
        (private / name).mkdir(parents=True, exist_ok=True)
    env.update(APPDATA=str(private / 'appdata'), LOCALAPPDATA=str(private / 'localappdata'),
               XDG_CACHE_HOME=str(private / 'cache'), XDG_CONFIG_HOME=str(private / 'config'),
               XDG_DATA_HOME=str(private / 'data'), PIP_CACHE_DIR=str(private / 'pip'),
               PIP_TARGET=str(private / 'python'), PYTHONUSERBASE=str(private / 'python-user'),
               MPLCONFIGDIR=str(private / 'cache/matplotlib'),
               JUPYTER_CONFIG_DIR=str(private / 'config/jupyter'),
               npm_config_cache=str(private / 'npm'), HF_HOME=str(private / 'huggingface'),
               TORCH_HOME=str(private / 'torch'), PYTHONDONTWRITEBYTECODE='1',
               PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               PSModuleAnalysisCachePath=str(private / 'powershell-analysis'),
               POWERSHELL_TELEMETRY_OPTOUT='1', POWERSHELL_UPDATECHECK='Off')
    from packages.runtime import resource_path
    python = resource_path('data_python', environment=(
        'CLEAR_GENERAL_PYTHON' if os.environ.get('CLEAR_GENERAL_PYTHON') else 'WORKBENCH_DATA_PYTHON'))
    python = require_f_path(python)
    if python.is_file():
        from .general_python import prepare
        python = prepare(runtime.store.root / 'harness', python)
        env['PATH'] = str(python.parent) + os.pathsep + env.get('PATH', '')
        state['python_executable'] = str(python)
        state['python_packages'] = str(private / 'python')
    return root


def catalog(runtime, state, schemas):
    if not enabled(state):
        raise Conflict('Legacy execution has no native general tool surface')
    state['native_tools'] = {tool['name']: tool for tool in schemas}
    runtime.graph.save(state)


def call_identity(state, params):
    call_id = params['call_id']
    if params.get('agent_session_id', state['session_id']) != state['session_id']:
        call_id = 'child_' + digest([params['agent_session_id'], call_id])[:48]
    return {'call_id': call_id, 'tool': params['tool'], 'arguments': params['arguments']}


def denial(code, message):
    return {'isError': True, 'content': [{'type': 'text', 'text': message}],
            'error': {'name': 'ClearExecutionDenied', 'code': code, 'reason': message}}


def begin(runtime, state, params):
    runtime.current(state)
    if not enabled(state) or params['tool'] not in state.get('native_tools', {}):
        return {'outcome': denial('UNKNOWN_TOOL', 'This native tool is not available in the current execution.')}
    additions, _ = runtime.inbox(state)
    if additions or runtime.tasks.get(state['session_id']).get('pending_scope'):
        return {'outcome': denial('SUPERSEDED', 'New user input or scope is pending; read the updated goal before acting.')}
    call = call_identity(state, params)
    context = runtime.context(state)
    if call['tool'] not in READ_TOOLS and 'derive' not in context.grants:
        return {'outcome': denial('SCOPE_DENIED', 'The current task does not allow creating or changing files.')}
    record, fresh = runtime.store.begin_operation(state['run_id'], state['generation'], call)
    if not fresh and record.get('result'):
        result = json.loads(record['result'])
        return {'outcome': result.get('data', {}).get('native_outcome') or denial(
            'RESPONSE_UNKNOWN', 'The original operation has an unresolved receipt; do not repeat its write.')}
    if not fresh and call['tool'] not in READ_TOOLS:
        return {'outcome': denial('RESPONSE_UNKNOWN', 'The original write receipt is unknown; inspect existing output before retrying.')}
    from dataclasses import replace
    tool_activity(replace(context, operation_id=record['id']), call, 'started')
    if fresh:
        state['tool_count'] += 1
        with runtime.store.transaction() as db:
            db.execute('UPDATE runs SET tool_count=? WHERE id=? AND generation=?',
                       (state['tool_count'], state['run_id'], state['generation']))
    runtime.activity(state, 'tool_dispatch', tool=call['tool'], call_id=call['call_id'])
    runtime.graph.save(state)
    return {'accepted': True, 'operation_id': record['id']}


def finish(runtime, state, params):
    runtime.current(state)
    call = call_identity(state, params)
    rows = runtime.store.rows('SELECT * FROM operations WHERE run_id=? AND call_id=?', (state['run_id'], call['call_id']))
    if not rows:
        raise Conflict('Native result lacks an accepted operation')
    outcome = params['outcome']
    text = '\n'.join(item.get('text', '') for item in outcome.get('content', []) if item.get('type') == 'text')
    failed = outcome.get('isError', False)
    result = {'status': 'error' if failed else 'success', 'summary': (text or call['tool'])[:2000],
              'data': {'native_outcome': outcome}, 'result_refs': [], 'artifact_refs': [], 'job_refs': []}
    if failed:
        result['error'] = {'code': str(outcome.get('error', {}).get('code', 'native_tool_failed'))[:80],
                           'message': text[:500] or 'Native tool did not complete', 'retryable': False}
    runtime.store.finish_operation(rows[0]['id'], result)
    from dataclasses import replace
    tool_activity(replace(runtime.context(state), operation_id=rows[0]['id']), call, 'finished', result)
    if not failed and call['tool'] not in READ_TOOLS:
        state['business_started'] = True
    runtime.graph.save(state)
    return {'saved': True}
