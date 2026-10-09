"""Measure the last delivery request using a historical actual export size witness.

This is a CPU size projection, not execution or evidence that the witness
belongs to the newer session. All witness IDs and receipt bytes stay exact.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import sys
from common import digest, fpath, frozen_json, read_json


def execute(args):
    source, assembly, witness, output = map(fpath, (args.platform_source, args.assembly, args.export_witness, args.output))
    if output.exists():
        raise ValueError('Use a fresh F: reserve report')
    sys.path[:0] = [str(source), str(source / 'src')]
    from workbench_platform.agent.context import prepare_request, core_tools
    from workbench_platform.agent.registry import ToolRegistry
    from workbench_platform.agent.tools import context, data, audit, history, skills
    from workbench_platform.agent.matching import existing_ocr_progress
    from workbench_platform.agent.providers import ProviderFault
    registry = ToolRegistry()
    for module in (context, data, audit, history, skills):
        module.register(registry)
    value = read_json(assembly); messages = deepcopy(value['messages'])
    exported = next(item for item in read_json(witness)['traces'] if item['call']['tool'] == 'data_export')
    call, receipt = exported['call'], exported['terminal_receipt']
    if receipt['status'] != 'success':
        raise ValueError('Size witness must be an actual completed export')
    route = existing_ocr_progress(messages, [])
    route['phase'] = 'verified_delivery'; route['next_tools'] = ['delivery_publish']
    route['archive_tools'] = list(dict.fromkeys([*route['archive_tools'], 'results_to_input', 'audit_bind_evidence', 'audit_read_evidence', 'data_export']))
    facts = json.loads(messages[0]['content'].split('\n', 1)[1]); facts['tool_route'] = route
    messages[0]['content'] = messages[0]['content'].split('\n', 1)[0] + '\n' + json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    messages.extend([{'role': 'assistant', 'protocol': value['config']['protocol'], 'content': '', 'calls': [call]},
                     {'role': 'tool', 'call_id': call['call_id'], 'result': receipt}])
    candidates = list(dict.fromkeys([*value['visible'], 'delivery_publish']))
    # All candidate contracts are from the actual registered OCR/data task.
    registry.tools = {name: tool for name, tool in registry.tools.items() if name in candidates or name in core_tools(registry)}
    report = {'format': 'historical-export-receipt-delivery-budget-size-projection-v1',
        'assembly': str(assembly), 'assembly_sha256': digest(assembly),
        'actual_export_witness': str(witness), 'actual_export_witness_sha256': digest(witness),
        'actual_export_call_id': call['call_id'], 'witness_receipt_or_arguments_changed': False,
        'counterfactual_phase_and_receipt_size_only': True, 'witness_is_not_new_session_evidence': True,
        'controller_calls': 0, 'business_calls': 0, 'real_agent_chain_verified': False, 'helper_sha256': digest(__file__)}
    try:
        _, metrics, selected = prepare_request(value['config'], messages, registry, candidates,
            system=value['system'], system_components=value['sections'], protected=['delivery_publish'],
            reclaimable_core=tuple(n for n in core_tools(registry) if n not in {'tools_describe', 'session_read_history', 'delivery_publish'}),
            archive_tools=route['archive_tools'], retain_table_columns=False)
        report.update(status='fits', metrics=metrics, selected_tools=selected,
                      input_margin=metrics['available_input_tokens'] - metrics['input_tokens_estimate'])
    except ProviderFault as error:
        if error.code != 'context_limit':
            raise
        report.update(status='overflow', metrics=error.context_metrics, input_margin=-error.context_metrics['overflow_tokens'])
    frozen_json(output, report)
    print(report['status'], report['metrics']['input_tokens_estimate'], 'margin', report['input_margin'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('platform-source', 'assembly', 'export-witness', 'output'):
        parser.add_argument('--' + name, required=True)
    execute(parser.parse_args())
