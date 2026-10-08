"""Replay a real failed request budget from an isolated checkpoint/store copy."""
from __future__ import annotations
import argparse
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from common import digest, fpath, frozen_json, read_json


async def execute(args):
    source = fpath(args.platform_source)
    root, original = fpath(args.output_root), fpath(args.original_run)
    if root.exists():
        raise ValueError('Budget replay needs a fresh F: evidence root')
    root.mkdir(parents=True)
    temp = root / 'temp'; temp.mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME'):
        os.environ[name] = str(temp)
    os.environ['WORKBENCH_AGENT_LAYER'] = args.agent_layer
    sys.path[:0] = [str(source), str(source / 'src')]
    from workbench_platform.agent.dependencies import load_dependencies
    from workbench_platform.agent.runtime import AgentRuntime, CURRENT_FACTS_PREFIX
    from workbench_platform.agent.context import prepare_request, core_tools, skill_control_tools, wire_json
    from workbench_platform.agent.environment import compose, environment
    from workbench_platform.agent.providers import SYSTEM, ProviderFault
    from workbench_platform.agent.store import canonical
    from workbench_platform.store import WorkspaceStore
    from s07_agent_native_followup import BUSINESS_TOOLS, DISCOVERY_TOOLS
    from s07_native_read import inventory, cold_copy
    before = inventory(original)
    clone = root / 'agent'; clone.mkdir()
    copied = {}
    for name in ('operations.sqlite3', 'checkpoints.sqlite3'):
        source_db = original / 'platform/agent' / name
        copied[name] = cold_copy(source_db, clone / name)
        with sqlite3.connect((clone / name).as_uri() + '?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Isolated Agent database copy failed SQLite integrity')
    frozen_json(root / 'agent-copy-qualification.json', copied)
    cold_copy(original / 'platform/workspaces.sqlite3', root / 'workspaces.sqlite3')
    _, Saver = load_dependencies()
    async with Saver.from_conn_string(str(clone / 'checkpoints.sqlite3')) as saver:
        value = await saver.aget_tuple({'configurable': {'thread_id': args.run_id}})
        if value is None:
            raise ValueError('Actual checkpoint does not exist')
        state = deepcopy(value.checkpoint['channel_values'])
    runtime = AgentRuntime(clone, SimpleNamespace(workspace_store=WorkspaceStore(root / 'workspaces.sqlite3')))
    runtime.registry.tools = {name: spec for name, spec in runtime.registry.tools.items()
                              if name in BUSINESS_TOOLS | DISCOVERY_TOOLS}
    run = runtime.store.run(args.run_id)
    task = runtime.tasks.get(state['session_id'])
    config = read_json(original / 'actual-goal.json')['controller']
    snapshot = runtime.skills.request_snapshot(state['workspace_id'], state['session_id'])
    facts = runtime.session_facts(run, task=task, skill_snapshot=snapshot)
    values = environment(config, workspace_id=state['workspace_id'], session_id=state['session_id'],
        scope_revision=task['scope_revision'], manual=task['manual'],
        families=(name.split('_', 1)[0] for name in runtime.registry.tools))
    system, _, sections = compose(SYSTEM, config, environment_values=values,
        skill_prompt='[登记技能精简名录；正文按需启用]\n' + canonical(snapshot.catalog) + '\n' + snapshot.instructions)
    messages = [message for message in state['messages']
                if not message.get('id', '').endswith((':session-facts', ':current-facts'))]
    messages.insert(0, {'id': run['id'] + ':current-facts', 'role': 'user',
        'content': '[服务器工作事实；用户目标/规则是任务约束，操作状态为历史回执；继续前查询当前状态]\n' + canonical(facts)})
    allowed = snapshot.allowed_tools(state['visible'], core=skill_control_tools(runtime.registry))
    visible = [name for name in state['visible'] if name in allowed]
    protected = [name for name in state.get('protected_tools', ()) if name in allowed]
    screen_calls = {call['call_id'] for message in state['messages'] if message['role'] == 'assistant'
                    for call in message.get('calls', ()) if call['tool'] == 'ocr_screen_batch_run'}
    has_screen_job = bool(state.get('background_jobs')) or any(message['role'] == 'tool' and
        message.get('call_id') in screen_calls and message['result'].get('status') in {'success', 'pending'}
        for message in state['messages'])
    reclaimable = tuple(name for name in core_tools(runtime.registry) if name not in {
        'conversation_answer', 'tools_list', 'tools_describe', 'ask_user', 'session_read_history',
        'delivery_publish', 'ocr_screen_batch_export'}) if has_screen_job else ()
    frozen_json(root / 'actual-assembly.json', {'config': config, 'messages': messages, 'visible': visible,
        'protected': protected, 'reclaimable_core': reclaimable, 'system': system, 'sections': sections})
    original_messages = deepcopy(messages)
    old_path = fpath(args.old_context)
    spec = importlib.util.spec_from_file_location('workbench_platform.agent.before_context_replay', old_path)
    old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
    before_metrics = None
    try:
        old.prepare_request(config, messages, runtime.registry, visible, system=system,
                            protected=protected, system_components=sections, reclaimable_core=reclaimable)
    except ProviderFault as error:
        if error.code != 'context_limit':
            raise
        before_metrics = error.context_metrics
    if before_metrics is None:
        raise ValueError('Old actual-request budget no longer reproduces its failure')
    projected_messages = deepcopy(messages)
    if args.current_facts_prefix:
        projected_messages[0]['content'] = CURRENT_FACTS_PREFIX + canonical(facts)
    projected_original = deepcopy(projected_messages)
    payload, metrics, selected = prepare_request(config, projected_messages, runtime.registry, visible,
        system=system, protected=protected, system_components=sections, reclaimable_core=reclaimable)
    serialized = wire_json(payload)
    if (messages != original_messages or projected_messages != projected_original or
            metrics['input_tokens_estimate'] > config['context_cap'] - config['max_output_tokens']):
        raise ValueError('Budget compaction mutated history or exceeded the unchanged controller window')
    if args.current_facts_prefix:
        current = [item['content'] for item in payload['messages']
                   if item.get('content', '').startswith(CURRENT_FACTS_PREFIX)]
        if len(current) != 1:
            raise ValueError('Current runtime facts header changed during projection')
        restored_facts = json.loads(current[0].split('\n', 1)[1])
        by_message_id = {item.get('id'): item.get('content') for item in projected_messages}
        for record in [restored_facts.get('task', {}), *restored_facts.get('current_user_instructions', [])]:
            for key in ('goal', 'content'):
                reference = record.get(key + '_ref')
                if reference is None:
                    continue
                original_text = by_message_id.get(reference['message_id'])
                if (not isinstance(original_text, str) or
                        hashlib.sha256(original_text.encode('utf8')).hexdigest() != reference['sha256'] or
                        original_text not in [item.get('content') for item in payload['messages']]):
                    raise ValueError('Current facts user-constraint reference cannot restore its exact retained text')
                record.pop(key + '_ref')
                record[key] = original_text
        if restored_facts != facts:
            raise ValueError('Current facts changed after exact user-constraint reference reconstruction')
    goal = read_json(original / 'actual-goal.json')['goal']
    if goal not in serialized:
        # JSON escaping differs for quoted goals; compare decoded message content.
        if goal not in [item.get('content') for item in payload['messages']]:
            raise ValueError('Original full user constraints were not retained')
    operations = read_json(original / 'actual-agent-operations.json')
    for item in operations:
        if item['call_id'] not in serialized:
            raise ValueError('A real completed operation identity was lost')
        if item['tool'] == 'skills_read_resource':
            facts_messages = [entry for entry in payload['messages'] if entry.get('content', '').startswith('[服务器保存的工具事实')]
            actual = [entry for entry in __import__('json').loads(facts_messages[0]['content'].split('\n', 1)[1])
                      if entry['tool'] == 'skills_read_resource']
            if actual[0]['data'] != item['receipt']['data']:
                raise ValueError('Read skill resource rules were lost or changed')
        if item['tool'] == 'tools_list':
            for tool in item['receipt']['data']['tools']:
                if tool['name'] not in serialized:
                    raise ValueError('Actual unactivated discovered name was lost')
    if inventory(original) != before:
        raise ValueError('Original failed evidence changed during isolated CPU replay')
    frozen_json(root / 'after-payload.json', payload)
    report = {'status': 'pass', 'format': 'actual-controller-context-budget-replay-v1',
        'original_run': str(original), 'original_run_unchanged': True, 'run_id': args.run_id,
        'old_context_sha256': digest(old_path), 'new_context_sha256': digest(source / 'src/workbench_platform/agent/context.py'),
        'helper_sha256': digest(__file__),
        'before_metrics': before_metrics, 'after_metrics': metrics, 'selected_tools': selected,
        'checkpoint_source_sha256': before['platform/agent/checkpoints.sqlite3'],
        'exact_user_goal_retained': True, 'all_actual_operation_ids_retained': True,
        'unactivated_discovery_names_retained': True, 'fixed_skill_resource_bytes_retained': True,
        'current_runtime_facts_prefix_projected': args.current_facts_prefix,
        'historical_old_request_prefix_preserved': True,
        'real_controller_calls': 0, 'model_calls': 0, 'source_model_credentials_copied': False,
        'scope': 'Actual durable checkpoint and production request assembly on an isolated CPU clone; not a completed real Agent task'}
    frozen_json(root / 'context-budget-replay.json', report)
    print(before_metrics['input_tokens_estimate'], '->', metrics['input_tokens_estimate'], 'available', metrics['available_input_tokens'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('platform-source', 'old-context', 'original-run', 'run-id', 'output-root'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--agent-layer', default='F:/OCR/output/platform-agent-dev-20261004/dependencies/agent')
    parser.add_argument('--current-facts-prefix', action='store_true',
                        help='Project the current runtime facts header only in the new request; retain the historical old-budget assembly')
    asyncio.run(execute(parser.parse_args()))


if __name__ == '__main__':
    main()
