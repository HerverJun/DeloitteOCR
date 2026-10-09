"""CPU phase admission against a real failed classification checkpoint.

Only the sending acknowledgement is projected. Historical availability is a
recorded size witness, and no native service, business action or model runs.
"""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from types import SimpleNamespace
import sys
from common import digest, fpath, frozen_json, read_json
from s07_native_read import cold_copy, inventory
from s07_agent_native_followup import DISCOVERY_TOOLS, platform_binding
from s07_agent_classification import TOOLS


async def execute(args):
    source, original, root = map(fpath, (args.platform_source, args.original_run, args.output_root))
    if root.exists():
        raise ValueError('Phase admission needs a fresh F: output')
    root.mkdir(parents=True); temporary = root / 'temp'; temporary.mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME'):
        os.environ[name] = str(temporary)
    os.environ['WORKBENCH_AGENT_LAYER'] = str(fpath(args.agent_layer))
    sys.path[:0] = [str(source), str(source / 'src')]
    from workbench_platform.agent.dependencies import load_dependencies
    from workbench_platform.agent.runtime import AgentRuntime, CURRENT_FACTS_PREFIX
    from workbench_platform.agent.context import prepare_request
    from workbench_platform.agent.environment import compose, environment
    from workbench_platform.agent.providers import SYSTEM
    from workbench_platform.agent.store import canonical
    from workbench_platform.store import WorkspaceStore
    before, bound = inventory(original), platform_binding(source)
    clone = root / 'agent'; clone.mkdir()
    for name in ('operations.sqlite3', 'checkpoints.sqlite3'):
        cold_copy(original / 'platform/agent' / name, clone / name)
    cold_copy(original / 'platform/workspaces.sqlite3', root / 'workspaces.sqlite3')
    _, Saver = load_dependencies()
    async with Saver.from_conn_string(str(clone / 'checkpoints.sqlite3')) as saver:
        checkpoint = await saver.aget_tuple({'configurable': {'thread_id': args.run_id}})
        state = deepcopy(checkpoint.checkpoint['channel_values'])
    runtime = AgentRuntime(clone, SimpleNamespace(workspace_store=WorkspaceStore(root / 'workspaces.sqlite3')))
    try:
        runtime.registry.tools = {name: tool for name, tool in runtime.registry.tools.items() if name in TOOLS | DISCOVERY_TOOLS}
        operation = read_json(original / 'actual-agent-operations.json')[-1]
        if operation['tool'] != 'tools_describe':
            raise ValueError('Expected the actual final classification load acknowledgement')
        state['messages'] = [m for m in state['messages'] if not (m.get('role') == 'tool' and m.get('call_id') == operation['call_id'])]
        call = next(c for m in state['messages'] for c in m.get('calls', ()) if c['call_id'] == operation['call_id'])
        state['calls'] = [call]
        last_event = json.loads(runtime.store.rows("SELECT payload FROM events WHERE run_id=? AND kind='model_request' ORDER BY seq DESC LIMIT 1", (args.run_id,))[0]['payload'])
        state['visible'] = last_event['injection']['tools']
        state['protected_tools'] = []
        config = read_json(original / 'actual-goal.json')['controller']
        runtime.connection = SimpleNamespace(resolve=lambda *_: (config, 'unused-cpu-projection'))
        async def saved(_):
            return SimpleNamespace(values=deepcopy(state))
        runtime.graph = SimpleNamespace(aget_state=saved)
        ctx = replace(runtime.context(state), operation_id=operation['id'])
        descriptions = deepcopy(operation['receipt']['data']['tools'])
        for item in descriptions:
            item.pop('reason', None)
        result = await runtime.admit_tools(ctx, call['arguments']['names'], descriptions)
        if result['data']['requested_tools'] != call['arguments']['names']:
            raise ValueError('Phase admission changed the actual requested tools')
        snapshot = runtime.skills.request_snapshot(state['workspace_id'], state['session_id'])
        run = runtime.store.run(args.run_id); task = runtime.tasks.get(state['session_id'])
        facts = runtime.session_facts(run, task=task, skill_snapshot=snapshot)
        route = runtime.workflow_route(state, snapshot, facts)
        facts['tool_route'] = route
        values = environment(config, workspace_id=state['workspace_id'], session_id=state['session_id'],
            scope_revision=task['scope_revision'], manual=task['manual'], families=(n.split('_', 1)[0] for n in runtime.registry.tools))
        system, _, sections = compose(SYSTEM, config, environment_values=values,
            skill_prompt='[登记技能精简名录；正文按需启用]\n' + canonical(snapshot.catalog) + '\n' + snapshot.instructions)
        messages = [m for m in state['messages'] if not m.get('id', '').endswith((':session-facts', ':current-facts'))]
        messages.insert(0, {'role': 'user', 'id': args.run_id + ':current-facts', 'content': CURRENT_FACTS_PREFIX + canonical(facts)})
        messages.append({'role': 'tool', 'call_id': call['call_id'], 'result': result})
        activated = result['data']['activated_tools']
        visible, protected = runtime.phase_contracts([*state['visible'], *activated], snapshot, route, activated)
        payload, metrics, selected = prepare_request(config, messages, runtime.registry, visible,
            protected=protected, system=system, system_components=sections,
            reclaimable_core=runtime.route_reclaimable_core(route), archive_tools=route['archive_tools'],
            retain_table_columns=route.get('retain_table_columns', True))
        if not set(activated + route['next_tools']) <= set(selected):
            raise ValueError('A claimed activation or authorized phase schema was lost')
        if read_json(original / 'actual-goal.json')['goal'] not in [m.get('content') for m in payload['messages']]:
            raise ValueError('Original user constraints were lost')
        frozen_json(root / 'projected-admission-receipt.json', result)
        frozen_json(root / 'projected-next-payload.json', payload)
        frozen_json(root / 'phase-admission.json', {'format': 'actual-classification-phase-admission-cpu-projection-v1',
            'status': 'pass', 'original_run': str(original), 'run_id': args.run_id, 'source_files': bound,
            'old_activated_tools': operation['receipt']['data']['activated_tools'], 'new_activated_tools': activated,
            'protected_phase_tools': route['next_tools'], 'next_request_metrics': metrics,
            'next_request_input_margin': metrics['available_input_tokens'] - metrics['input_tokens_estimate'],
            'original_receipt_rewritten': False, 'counterfactual_acknowledgement_only': True,
            'captured_readiness_is_historical': True, 'all_original_user_constraints_preserved': True,
            'controller_calls': 0, 'business_calls': 0, 'gpu_calls': 0, 'real_agent_chain_verified': False,
            'helper_sha256': digest(__file__)})
        print('phase_admission', metrics['input_tokens_estimate'], 'margin', metrics['available_input_tokens'] - metrics['input_tokens_estimate'])
    finally:
        await runtime.close()
    if inventory(original) != before or platform_binding(source) != bound:
        raise ValueError('Original failed run or frozen source changed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('platform-source', 'original-run', 'output-root', 'run-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--agent-layer', default='F:/OCR/output/platform-agent-dev-20261004/dependencies/agent')
    asyncio.run(execute(parser.parse_args()))
