"""Run the unchanged seven-module Agent gate against an F: overlay freeze."""
from __future__ import annotations
import argparse
import contextlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from common import digest, fpath, frozen_json
from s07_agent_native_followup import platform_binding


def qualify(snapshot, output, temporary):
    snapshot, output, temporary = map(fpath, (snapshot, output, temporary))
    source = snapshot / 'platform'
    receipt = json.loads((snapshot / 'freeze.json').read_text('utf8'))
    before = platform_binding(source)
    if before != receipt['frozen_files']:
        raise ValueError('Snapshot production source differs before gate')
    if any(digest(source / name) != sha for name, sha in receipt['owned_overlay_files'].items()):
        raise ValueError('Snapshot owned sources/tests differ before gate')
    sys.path[:0] = [str(source), str(source / 'src'),
                   'F:/OCR/output/platform-agent-dev-20261004/dependencies/agent']
    import pytest
    names = ('test_context_budget', 'test_context_completed_batches', 'test_wire_context',
             'test_request_snapshot', 'test_tool_activation_budget',
             'test_specialized_matching_preferences', 'test_specialized_runtime',
             'test_specialized_terminal_delivery', 'test_screen_batch_wait')
    selected = [source / 'tests/agent' / (name + '.py') for name in names]
    output.parent.mkdir(parents=True, exist_ok=True)
    log, xml = output.with_suffix('.log'), output.with_suffix('.xml')
    if output.exists() or log.exists() or xml.exists():
        raise ValueError('Use a fresh immutable qualification stem')
    with log.open('w', encoding='utf8') as stream, contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
        code = pytest.main([*(str(path) for path in selected), '-q', '-p', 'no:cacheprovider',
                           '--basetemp=' + str(temporary), '--junitxml=' + str(xml)])
    unchanged = platform_binding(source) == before and all(
        digest(source / name) == sha for name, sha in receipt['owned_overlay_files'].items())
    report = {'format': 's07-agent-stages-frozen-seven-module-gate-v1',
        'status': 'pass' if code == 0 and unchanged else 'fail', 'exit_code': code,
        'snapshot': str(snapshot), 'snapshot_sha256': digest(snapshot / 'freeze.json'),
        'source_files': before, 'owned_overlay_files': receipt['owned_overlay_files'],
        'selected_tests': [path.relative_to(source).as_posix() for path in selected],
        'source_unchanged': unchanged, 'junit_suites': [dict(item.attrib) for item in ET.parse(xml).getroot().iter('testsuite')],
        'log_sha256': digest(log), 'xml_sha256': digest(xml), 'helper_sha256': digest(__file__),
        'controller_calls': 0, 'gpu_calls': 0, 'real_agent_chain_verified': False}
    frozen_json(output, report)
    print(report['status'], report['junit_suites'])
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot', 'output', 'temporary'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    raise SystemExit(qualify(args.snapshot, args.output, args.temporary))
