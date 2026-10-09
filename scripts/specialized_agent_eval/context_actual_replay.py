"""Replay a real failed request budget from an isolated checkpoint/store copy."""
from __future__ import annotations
import argparse
import ast
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from common import digest, fpath, frozen_json, read_json


def verify_archived_facts(payload, operations):
    """Reconstruct sending-only indexes against exact durable operation receipts."""
    from workbench_platform.agent.context import wire_json
    summaries = [message['content'] for message in payload['messages']
                 if message.get('content', '').startswith('[服务器保存的工具事实')]
    if len(summaries) != 1:
        raise ValueError('Actual replay requires one complete operation fact index')
    facts = json.loads(summaries[0].split('\n', 1)[1])
    by_id = {fact['call_id']: index for index, fact in enumerate(facts)}
    if len(by_id) != len(facts):
        raise ValueError('Archived fact call identities are not unique')

    def field_at(position, key, visited=()):
        identity = (position, key)
        if identity in visited or not 0 <= position < len(facts):
            raise ValueError('Archived fact index cycles or escapes its source')
        fact = facts[position]
        if key in fact:
            return deepcopy(fact[key])
        reference = fact.get(key + '_ref')
        if type(reference) is int:
            source = reference
        elif isinstance(reference, dict) and key == 'data':
            source = by_id[reference['call_id']]
        else:
            return None
        if source >= position:
            raise ValueError('Archived field source is not a preceding fact')
        restored = field_at(source, key, (*visited, identity))
        if isinstance(reference, dict) and hashlib.sha256(wire_json(restored).encode('utf8')).hexdigest() != reference['sha256']:
            raise ValueError('Archived field source hash differs')
        return restored

    fields = {'name', 'version', 'schema_sha256', 'availability', 'allowed', 'loaded', 'error', 'reason'}

    def tool_at(position, index, visited=()):
        identity = (position, index)
        if identity in visited:
            raise ValueError('Archived metadata index cycles')
        data = field_at(position, 'data') or {}
        item = data['tools'][index]
        restored = {}
        for key in ('metadata_ref', 'availability_ref'):
            reference = item.get(key)
            if reference is None:
                continue
            if isinstance(reference, list) and len(reference) == 2 and all(type(part) is int for part in reference):
                source, source_index = reference
            elif isinstance(reference, dict):
                source = by_id[reference['call_id']]
                source_index = reference.get('tool_index')
                if source_index is None:
                    source_index = next(index for index, entry in enumerate(field_at(source, 'data')['tools'])
                                        if entry['name'] == reference['tool_name'])
            else:
                raise ValueError('Archived metadata source has an unknown representation')
            if source > position or source == position and source_index >= index:
                raise ValueError('Archived metadata source does not precede the item')
            prior = tool_at(source, source_index, (*visited, identity))
            if key == 'metadata_ref':
                restored.update({name: prior[name] for name in ('name', 'version', 'schema_sha256', 'availability') if name in prior})
            else:
                restored['availability'] = prior['availability']
        restored.update(deepcopy(data.get('tool_defaults', {})))
        restored.update({name: deepcopy(value) for name, value in item.items() if name in fields})
        return restored

    archived_items = 0
    archived_business = 0
    visible_readbacks = 0
    for operation in operations:
        position = by_id[operation['call_id']]
        fact, receipt = facts[position], operation['receipt']
        opaque = fact.get('archived_operation_sha256')
        if opaque and opaque != hashlib.sha256(json.dumps({
                'arguments': operation['arguments'], 'result': receipt}, sort_keys=True,
                ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest():
            raise ValueError('Archived original command/source receipt identity changed')
        arguments = operation['arguments'] if opaque else fact['arguments']
        if fact['tool'] != operation['tool'] or arguments != operation['arguments'] or fact['status'] != receipt['status']:
            raise ValueError('Archived call arguments, tool or status changed')
        for key in ('result_refs', 'job_refs', 'artifact_refs', 'warnings', 'error', 'coverage'):
            if opaque and key in {'result_refs', 'job_refs', 'artifact_refs'}:
                continue
            if field_at(position, key) != receipt.get(key) and (field_at(position, key) or receipt.get(key)):
                raise ValueError('Archived operation references or risk changed: ' + key)
        data = field_at(position, 'data') or {}
        visible = data.get('verified_readback')
        if visible:
            page = receipt['data']
            original = json.loads(page['content'])
            source = original['result']
            readback_fields = visible['restored_fields']
            if (visible['source_call_id'] != page['call_id'] or visible['source_tool'] != page.get('tool')
                    or visible['source_result_sha256'] != page['result_sha256']
                    or visible['source_operation_sha256'] != page['content_sha256']
                    or readback_fields['status'] != source['status']):
                raise ValueError('Latest visible operation readback changed its identity or state')
            for key in ('artifact_refs', 'result_refs', 'warnings', 'error'):
                if readback_fields.get(key) != source.get(key) and (readback_fields.get(key) or source.get(key)):
                    raise ValueError('Latest visible readback lost an exact source reference or risk')
            bundle = source.get('data', {}).get('bundle')
            if isinstance(bundle, dict):
                expected = [{key: item[key] for key in ('name', 'size_bytes', 'availability') if key in item}
                            for item in bundle.get('files', [])]
                if readback_fields['files'] != expected or readback_fields['bundle']['bundle_id'] != bundle['bundle_id']:
                    raise ValueError('Latest visible readback changed actual delivered file metadata')
            visible_readbacks += 1
        if fact.get('data_preview_only'):
            expected = hashlib.sha256(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()
            if fact.get('archived_receipt_sha256') != expected or receipt['status'] != 'success':
                raise ValueError('Archived completed business receipt hash/status changed')
            def risks(item):
                if isinstance(item, dict):
                    names = {'warning', 'warnings', 'error', 'reason', 'message', 'outdated_reasons'}
                    return {key: deepcopy(value) if key in names else nested for key, value in item.items()
                            if (key in names and value) or (key not in names and (nested := risks(value)))}
                if isinstance(item, list):
                    return [nested for value in item if (nested := risks(value))]
                return {}
            if data.get('retained_risks', {}) != risks(receipt.get('data', {})):
                raise ValueError('Archived nested business risks changed')
            archived_business += 1
        if operation['tool'] not in {'tools_list', 'tools_describe'} or not data.get('archived_receipt_sha256'):
            continue
        expected = hashlib.sha256(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False).encode('utf8')).hexdigest()
        if data['archived_receipt_sha256'] != expected:
            raise ValueError('Archived complete discovery receipt identity changed')
        for index, original in enumerate(receipt['data']['tools']):
            if tool_at(position, index) != {key: deepcopy(value) for key, value in original.items() if key in fields}:
                raise ValueError('Archived permission, load, error or metadata changed')
            archived_items += 1
    return {'exact_operation_facts': len(operations), 'exact_archived_tool_entries': archived_items,
            'exact_archived_business_receipts': archived_business,
            'exact_visible_complete_readbacks': visible_readbacks,
            'permissions_and_loading_are_local': True, 'source_versions_and_receipt_hashes_preserved': True}


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
    from s07_agent_classification import TOOLS as CLASSIFICATION_TOOLS
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
    classification = (original / 'result.json').is_file() and read_json(original / 'result.json').get(
        'format') == 'actual-agent-classification-result-v1'
    business_tools = CLASSIFICATION_TOOLS if classification else BUSINESS_TOOLS
    runtime.registry.tools = {name: spec for name, spec in runtime.registry.tools.items()
                              if name in business_tools | DISCOVERY_TOOLS}
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
    original_prefix = (CURRENT_FACTS_PREFIX if args.original_facts_prefix == 'current' else
        '[服务器工作事实；用户目标/规则是任务约束，操作状态为历史回执；继续前查询当前状态]\n')
    messages.insert(0, {'id': run['id'] + ':current-facts', 'role': 'user',
        'content': original_prefix + canonical(facts)})
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
    original_messages = deepcopy(messages)
    old_path = fpath(args.old_context)
    spec = importlib.util.spec_from_file_location('workbench_platform.agent.before_context_replay', old_path)
    old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
    old_extra = {}
    if args.workflow_stages:
        # Reproduce the actual historical phase with its frozen source, then
        # measure current production stage assembly at the same checkpoint.
        matching_path = old_path.with_name('matching.py')
        old_spec = importlib.util.spec_from_file_location('actual_before_matching', matching_path)
        old_matching = importlib.util.module_from_spec(old_spec); old_spec.loader.exec_module(old_matching)
        original_route = (old_matching.classification_progress(state['messages']) if classification else
            old_matching.existing_ocr_progress(state['messages'], runtime.store.session(state['session_id'])['initial_refs']))
        old_facts = deepcopy(facts); old_facts['tool_route'] = original_route
        messages[0]['content'] = original_prefix + canonical(old_facts)
        original_messages = deepcopy(messages)
        # Reproduce the historical runtime's schema protection, including
        # whether its final phase pinned the history reader. A newer host
        # policy must not shrink the failed baseline before measuring it.
        old_runtime = old_path.with_name('runtime.py')
        tree = ast.parse(old_runtime.read_text('utf8'))
        runtime_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'AgentRuntime')
        method = next(node for node in runtime_class.body if isinstance(node, ast.FunctionDef) and node.name == 'route_reclaimable_core')
        namespace = {'core_tools': core_tools}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(old_runtime), 'exec'), namespace)
        reclaimable = namespace['route_reclaimable_core'](runtime, original_route)
        # The historical runtime already provided each authorized stage's
        # next contract. Reproduce that exact schema selection and archive
        # policy, rather than comparing against a larger synthetic baseline.
        old_candidates = list(dict.fromkeys([*visible, *original_route['next_tools']]))
        old_allowed = snapshot.allowed_tools(old_candidates, core=skill_control_tools(runtime.registry))
        visible = [name for name in old_candidates if name in old_allowed and name in runtime.registry.tools]
        protected = list(dict.fromkeys([*protected, *[name for name in original_route['next_tools'] if name in visible]]))
        parameters = inspect.signature(old.prepare_request).parameters
        if 'archive_tools' in parameters:
            old_extra['archive_tools'] = original_route.get('archive_tools', ())
        if 'retain_table_columns' in parameters:
            old_extra['retain_table_columns'] = original_route.get('retain_table_columns', True)
    frozen_json(root / 'actual-assembly.json', {'config': config, 'messages': messages, 'visible': visible,
        'protected': protected, 'reclaimable_core': reclaimable, 'historical_stage_options': old_extra,
        'system': system, 'sections': sections})
    before_metrics = None
    try:
        old.prepare_request(config, messages, runtime.registry, visible, system=system,
                            protected=protected, system_components=sections, reclaimable_core=reclaimable, **old_extra)
    except ProviderFault as error:
        if error.code != 'context_limit':
            raise
        before_metrics = error.context_metrics
    if before_metrics is None:
        raise ValueError('Old actual-request budget no longer reproduces its failure')
    projected_messages = deepcopy(messages)
    projected_facts = deepcopy(facts)
    current_visible, current_protected = visible, protected
    current_reclaimable = reclaimable
    archive_tools = ()
    if args.workflow_stages:
        route = runtime.workflow_route(state, snapshot, facts)
        if route is None:
            raise ValueError('Current fixed Skill does not support this actual workflow checkpoint')
        projected_facts['tool_route'] = route
        candidates = list(dict.fromkeys([*visible, *route['next_tools']]))
        current_allowed = snapshot.allowed_tools(candidates, core=skill_control_tools(runtime.registry))
        current_visible = [name for name in candidates if name in current_allowed and name in runtime.registry.tools]
        current_protected = list(dict.fromkeys([*protected, *[name for name in route['next_tools'] if name in current_visible]]))
        archive_tools = route['archive_tools']
        current_reclaimable = runtime.route_reclaimable_core(route)
        projected_messages[0]['content'] = original_prefix + canonical(projected_facts)
    if args.current_facts_prefix:
        projected_messages[0]['content'] = CURRENT_FACTS_PREFIX + canonical(projected_facts)
    projected_original = deepcopy(projected_messages)
    payload, metrics, selected = prepare_request(config, projected_messages, runtime.registry, current_visible,
        system=system, protected=current_protected, system_components=sections, reclaimable_core=current_reclaimable,
        archive_tools=archive_tools,
        retain_table_columns=projected_facts.get('tool_route', {}).get('retain_table_columns', True))
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
        if restored_facts != projected_facts:
            raise ValueError('Current facts changed after exact user-constraint reference reconstruction')
    goal = read_json(original / 'actual-goal.json')['goal']
    if goal not in serialized:
        # JSON escaping differs for quoted goals; compare decoded message content.
        if goal not in [item.get('content') for item in payload['messages']]:
            raise ValueError('Original full user constraints were not retained')
    operations = read_json(original / 'actual-agent-operations.json')
    reconstructed = verify_archived_facts(payload, operations)
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
        'old_runtime_sha256': digest(old_path.with_name('runtime.py')) if args.workflow_stages else None,
        'helper_sha256': digest(__file__),
        'before_metrics': before_metrics, 'after_metrics': metrics, 'selected_tools': selected,
        'checkpoint_source_sha256': before['platform/agent/checkpoints.sqlite3'],
        'exact_user_goal_retained': True, 'all_actual_operation_ids_retained': True,
        'unactivated_discovery_names_retained': True, 'fixed_skill_resource_bytes_retained': True,
        'indexed_facts_reconstruction': reconstructed,
        'current_runtime_facts_prefix_projected': args.current_facts_prefix,
        'current_workflow_stages_projected': args.workflow_stages,
        'workflow': 'classification' if classification else 'existing_ocr_reconciliation',
        'original_runtime_facts_prefix': args.original_facts_prefix,
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
    parser.add_argument('--workflow-stages', action='store_true',
                        help='Reproduce the frozen historical phase and apply current authorized stage contracts/receipt archives')
    parser.add_argument('--original-facts-prefix', choices=('legacy', 'current'), default='legacy',
                        help='Use the historically sent facts header when reproducing the original failed assembly')
    asyncio.run(execute(parser.parse_args()))


if __name__ == '__main__':
    main()
