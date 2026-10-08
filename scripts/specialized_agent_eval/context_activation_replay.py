"""Counterfactual CPU admission on an immutable failed Agent checkpoint copy.

This replaces only the old tools_describe activation acknowledgement in the
sending projection. It is neither a completed real Agent task nor a mutation
of the original durable receipt. The exact requested names are preserved.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from common import digest, fpath, frozen_json, read_json
from s07_native_read import cold_copy, inventory
from s07_agent_native_followup import BUSINESS_TOOLS, DISCOVERY_TOOLS, platform_binding


async def execute(args):
    source, original, assembly = map(fpath, (args.platform_source, args.original_run, args.assembly))
    root = fpath(args.output_root)
    if root.exists():
        raise ValueError('CPU admission needs a new F: evidence root')
    root.mkdir(parents=True)
    temporary = root / 'temp'; temporary.mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME'):
        os.environ[name] = str(temporary)
    os.environ['WORKBENCH_AGENT_LAYER'] = str(fpath(args.agent_layer))
    sys.path[:0] = [str(source), str(source / 'src')]
    from workbench_platform.agent.runtime import AgentRuntime
    from workbench_platform.agent.context import prepare_request, wire_json
    from workbench_platform.store import WorkspaceStore
    before, bound = inventory(original), platform_binding(source)
    copy_root = root / 'agent'; copy_root.mkdir()
    for name in ('operations.sqlite3', 'checkpoints.sqlite3'):
        cold_copy(original / 'platform/agent' / name, copy_root / name)
    cold_copy(original / 'platform/workspaces.sqlite3', root / 'workspaces.sqlite3')
    runtime = AgentRuntime(copy_root, SimpleNamespace(workspace_store=WorkspaceStore(root / 'workspaces.sqlite3')))
    runtime.registry.tools = {name: tool for name, tool in runtime.registry.tools.items()
                              if name in BUSINESS_TOOLS | DISCOVERY_TOOLS}
    value = read_json(assembly)
    messages = deepcopy(value['messages'])
    latest = messages[-1]
    if latest['role'] != 'tool':
        raise ValueError('Actual failed checkpoint has no completed load reply')
    calls = {call['call_id']: call for message in messages for call in message.get('calls', [])}
    call = calls.get(latest['call_id'], {})
    if call.get('tool') != 'tools_describe':
        raise ValueError('CPU admission is limited to the captured final load request')
    run = runtime.store.run(args.run_id)
    # Only the isolated CPU clone becomes current; no controller/credentials
    # are copied or configured, and no graph is started.
    with runtime.store.transaction() as db:
        db.execute("UPDATE runs SET status='running',error=NULL WHERE id=?", (run['id'],))
    state = {'run_id': run['id'], 'workspace_id': run['workspace_id'], 'session_id': run['session_id'],
             'generation': run['generation'], 'step': 0}
    result = await runtime.admit_tools(runtime.context(state), call['arguments']['names'], latest['result']['data']['tools'])
    if result.get('error', {}).get('code') != 'skill_tool_denied' or result['data']['activated_tools']:
        raise ValueError('Captured mixed Skill deny was not retained with zero claimed activation')
    if result['data']['requested_tools'] != call['arguments']['names']:
        raise ValueError('CPU admission changed original requested names')
    previous = set(latest['result']['data']['activated_tools'])
    messages[-1]['result'] = result
    visible = [name for name in value['visible'] if name not in previous]
    payload, metrics, selected = prepare_request(value['config'], messages, runtime.registry, visible,
        protected=[], system=value['system'], system_components=value['sections'])
    serialized = wire_json(payload)
    if 'skills_deactivate' not in selected:
        raise ValueError('Explicit Skill exit contract was lost')
    facts = __import__('json').loads(next(message['content'] for message in payload['messages']
        if message.get('content', '').startswith('[服务器保存的工具事实')).split('\n', 1)[1])
    by_id = {item['call_id']: item for item in facts}
    for message in messages:
        if message['role'] == 'tool':
            if message['call_id'] not in by_id:
                raise ValueError('Actual operation identity was lost')
            if by_id[message['call_id']]['tool'] == 'skills_read_resource' and by_id[message['call_id']]['data'] != message['result']['data']:
                raise ValueError('Fixed resource bytes were changed')
    goal = read_json(original / 'actual-goal.json')['goal']
    if goal not in [message.get('content') for message in payload['messages']]:
        raise ValueError('Original full user constraints were lost')
    frozen_json(root / 'sending-projection.json', payload)
    frozen_json(root / 'new-admission-receipt.json', result)
    if inventory(original) != before or platform_binding(source) != bound:
        raise ValueError('Original failed run or frozen platform source changed')
    report = {'format': 'actual-failed-load-cpu-readmission-v1', 'status': 'cpu_scope_denial_and_budget_verified',
        'original_run': str(original), 'original_run_unchanged': True, 'run_id': run['id'],
        'captured_assembly': str(assembly), 'assembly_sha256': digest(assembly),
        'source_files': bound, 'next_request_metrics': metrics, 'actual_requested_names_preserved': True,
        'all_actual_operation_ids_retained': True, 'exact_user_goal_retained': True,
        'fixed_skill_resource_bytes_retained': True, 'explicit_skill_exit_remains_available': True,
        'activated_tools': [], 'model_calls': 0, 'controller_calls': 0, 'gpu_calls': 0,
        'counterfactual_acknowledgement_only': True, 'original_operation_receipt_rewritten': False,
        'captured_readiness_is_historical': True, 'real_agent_chain_verified': False,
        'helper_sha256': digest(__file__), 'python': str(fpath(sys.executable))}
    frozen_json(root / 'cpu-admission-result.json', report)
    await runtime.close()
    print(report['status'], metrics['input_tokens_estimate'], '/', metrics['available_input_tokens'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('platform-source', 'original-run', 'assembly', 'run-id', 'output-root'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--agent-layer', default='F:/OCR/output/platform-agent-dev-20261004/dependencies/agent')
    asyncio.run(execute(parser.parse_args()))
