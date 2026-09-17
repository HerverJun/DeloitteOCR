"""Collect existing audit evidence; no inference or product mutations."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


def read(name):
    return json.loads((ROOT / name).read_text('utf-8'))


ui_files = ['ui-multimodal/ui-report.json', 'ui-document-scoped/ui-results.json',
            'ui-structure/report.json', 'ui-tool-recovery/report.json']
ui = []
for name in ui_files:
    value = read(name)
    assert not value.get('errors'), (name, value.get('errors'))
    assert value['browser_closed']
    assert all(check['passed'] for check in value['checks'])
    ui.append({'evidence': name, 'checks_passed': len(value['checks']), 'browser_closed': True})

snapshot = read('source-snapshot.json')
changed = [row['path'] for row in snapshot
           if hashlib.sha256((REPO / row['path']).read_bytes()).hexdigest() != row['sha256']]
assert not changed, changed

report = (ROOT / 'REPORT.md').read_text('utf-8')
broken_links = [target for target in re.findall(r'\]\(([^)]+)\)', report)
                if not (ROOT / target).exists()]
assert not broken_links, broken_links

extended = read('ui-extended-verified/extended-results.json')
assert len(extended['findings']) == 3 and all(row['reproduced'] for row in extended['findings'])
assert not extended['pageErrors'] and extended['browserClosed']
four = read('four-engines/application-audit.json')
assert four['passed'] and len(four['engines']) == 4
review = read('gpu-review/receipt.json')
full = read('gpu-full-page/receipt.json')
cancel = read('gpu-cancel/receipt.json')
assert review['task']['status'] == full['task']['status'] == 'succeeded'
assert cancel['task']['status'] == 'cancelled' and not cancel['view']['proposals']
for value in [review, full, cancel]:
    assert value['original_and_edited_unchanged_after_inference']

summary = {
    'date': '2026-09-17', 'application': '0.11.0rc1', 'schema': 11,
    'audit_complete': True, 'product_defects_fixed': False,
    'findings': {'total': 8, 'P1': 1, 'P2': 7},
    'backend_suite': {'run': 383, 'passed': 382, 'skipped': 1, 'seconds': 146.977},
    'frontend_suite': {'passed': 39, 'build_passed': True},
    'normal_browser_checks_passed': sum(row['checks_passed'] for row in ui), 'browser_suites': ui,
    'browser_defects_reproduced': len(extended['findings']),
    'four_real_engines': four['engines'],
    'real_review': {'localized_seconds': review['wall_seconds'], 'localized_peak_device_mib': review['peak_device_memory_mib'],
                    'full_page_seconds': full['wall_seconds'], 'full_page_peak_device_mib': full['peak_device_memory_mib'],
                    'full_page_spurious_change': {'before': '数量', 'after': '00075'},
                    'cancel_seconds': cancel['cancel_seconds']},
    'source_files_unchanged': len(snapshot), 'report_links_verified': True,
    'important_limits': ['Synthetic GPU fixtures are not independent quality validation',
                         'Target RTX A4000 and another Windows machine were not tested',
                         'No new system-level airgap or long-duration stress test',
                         'Quick engine checks do not prove cross-task resident session reuse'],
}
(ROOT / 'audit-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), 'utf-8')
print(json.dumps({'findings': summary['findings'], 'normal_browser_checks': summary['normal_browser_checks_passed'],
                  'unchanged_source_files': len(snapshot), 'broken_report_links': broken_links}, ensure_ascii=False))
