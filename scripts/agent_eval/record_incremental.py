"""Record completed development checks without converting them to phase acceptance."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / 'audit/ocr-agent-20260921-langgraph'


def record():
    checks = {}
    names = ('inbox-lifecycle-tests', 'queue-recovery-tests', 'child-budget-tests', 'artifacts-child-regression',
             'correction-regression', 'partial-export-tests', 'agent-latest-regression', 'full-backend-fixes', 'ui-api-tests',
             'full-backend-regression', 'batch-error-tests', 'rich-results-tests', 'resume-reconciliation-tests',
             'ui-recovery-regression', 'no-progress-tests', 'target-pagination-tests',
             'full-backend-current', 'late-cancel-tests', 'artifact-boundary-tests', 'pdf-artifact-tests',
             'checkpoint-fault-tests', 'maintenance-20260923-tests')
    optional_names = ('context-semantic-regression', 'large-visual-final-tests', 'large-visual-regression-tests',
                      'protocol-boundaries-20260923-tests', 'artifact-complete-tests',
                      'maintenance-regression-fix-20260923', 'maintenance-related-regression-fix-20260923',
                      'full-backend-revalidated-20260923')
    names += tuple(name for name in optional_names if (AUDIT / (name + '.log')).exists())
    for name in names:
        raw = (AUDIT / (name + '.log')).read_bytes()
        content = raw.decode('utf-16' if raw.startswith(b'\xff\xfe') else 'utf-8', errors='replace')
        match = re.search(r'Ran (\d+) tests? in ([\d.]+)s', content)
        skipped = re.search(r'OK \(skipped=(\d+)\)', content)
        checks[name] = {'status': 'pass' if match and re.search(r'\nOK(?: \(skipped=\d+\))?\s*$', content) else 'fail',
                        'skipped': int(skipped[1]) if skipped else 0,
                        'tests': int(match[1]) if match else None, 'seconds': float(match[2]) if match else None,
                        'passed': int(match[1]) - (int(skipped[1]) if skipped else 0) if match else None,
                        'log': name + '.log', 'sha256': hashlib.sha256(raw).hexdigest()}
    raw = (AUDIT / 'frontend-build.log').read_bytes()
    checks['frontend-build'] = {'status': 'pass' if 'built in' in raw.decode('utf-16' if raw.startswith(b'\xff\xfe') else 'utf-8', errors='replace') else 'fail',
                                'log': 'frontend-build.log', 'sha256': hashlib.sha256(raw).hexdigest()}
    raw = (AUDIT / 'frontend-tests.log').read_bytes()
    content = raw.decode('utf-16' if raw.startswith(b'\xff\xfe') else 'utf-8', errors='replace')
    tests = re.search(r'Tests\s+(\d+) passed', content)
    checks['frontend-tests'] = {'status': 'pass' if tests and not re.search(r'\d+ failed', content) else 'fail',
                               'tests': int(tests[1]) if tests else None, 'log': 'frontend-tests.log', 'sha256': hashlib.sha256(raw).hexdigest()}
    for name, path in [('browser-qa', 'browser-qa-09/receipt.json'), ('scale-smoke', 'performance-02/performance.json')]:
        raw = (AUDIT / path).read_bytes()
        receipt = json.loads(raw)
        checks[name] = {'status': 'pass' if receipt.get('passed') or receipt.get('status') == 'pass' else 'fail',
                        'log': path, 'sha256': hashlib.sha256(raw).hexdigest()}
    frontend_final = AUDIT / 'frontend-final-20260923.json'
    if frontend_final.exists():
        frontend_receipt = json.loads(frontend_final.read_text('utf-8'))
        checks['frontend-final-20260923'] = {'status': 'pass' if frontend_receipt.get('passed') else 'fail', 'log': frontend_final.name,
            'sha256': hashlib.sha256(frontend_final.read_bytes()).hexdigest(),
            'scope': '51 unit checks and 19 external browser behavior checks; exact hashes and exit codes in receipt'}
    for label, relative in [('browser-qa-15', 'browser-qa-15/verification.json'),
                            ('gpu-batch-02', 'gpu-batch-02/gpu-batch.json'),
                            ('gpu-ui-03', 'gpu-ui-03/verification.json'),
                            ('documentation-review', 'documentation-review.json')]:
        path = AUDIT / relative
        if not path.exists():
            continue
        raw = path.read_bytes()
        receipt = json.loads(raw)
        checks[label] = {'status': 'pass' if receipt.get('passed') or receipt.get('status') in
                         {'pass', 'pass_with_explicit_qualification_limits'} else 'fail',
                         'log': relative, 'sha256': hashlib.sha256(raw).hexdigest(),
                         'scope': {'browser-qa-15': '10 synthetic-controller browser check groups',
                                   'gpu-batch-02': '20-page real local GLM OCR to XLSX, synthetic controller',
                                   'gpu-ui-03': 'two Edge viewport history and same-file XLSX download after project-bound artifact API',
                                   'documentation-review': 'G01 topics and local links, no model qualification'}[label]}
    stability_path = AUDIT / 'stability-03/performance.json'
    stability = json.loads(stability_path.read_text('utf-8')) if stability_path.exists() else {}
    mixed_path = AUDIT / 'stability.json'
    mixed = json.loads(mixed_path.read_text('utf-8')) if mixed_path.exists() else {}
    candidate02 = AUDIT / 'bundle-build-02.json'
    package_receipt = json.loads(candidate02.read_text('utf-8')) if candidate02.exists() else json.loads((AUDIT / 'bundle-build.json').read_text('utf-8'))
    source_match = False
    if candidate02.exists():
        manifest_path = Path(package_receipt['bundle']) / 'source-manifest.json'
        if manifest_path.exists():
            source_map = json.loads(manifest_path.read_text('utf-8'))
            source_match = all((ROOT / name).is_file() and hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
                               for name, expected in source_map.items())
    paths = sorted(set(ROOT.glob('src/ocr_workbench/**/*.py')) | set(ROOT.glob('tests/test_agent*.py')) |
                   set(ROOT.glob('frontend/src/Agent*.tsx')) | set(ROOT.glob('docs/ocr-agent-usage.md')) |
                   set(ROOT.glob('docs/ocr-agent-operations.md')) | set(ROOT.glob('docs/ocr-agent-development-notes.md')))
    data = {'updated': datetime.now(timezone.utc).isoformat(), 'business_schema': 14,
            'scope': 'Development integration; full v2 phase and E01-E72 acceptance remain unfinished',
            'checks': checks, 'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            'real_controller': 'not_tested', 'target_machine': 'not_tested',
            'four_hour_stability': {'status': mixed.get('status', 'not_started'), 'report': 'stability.json',
                'scope': 'current-source real local GPU OCR, visual fixture, Agent, and two browser clients',
                'synthetic_older_track': {'status': stability.get('status', 'starting'), 'elapsed_seconds': stability.get('elapsed_soak_seconds', 0),
                'report': 'stability-03/performance.json', 'scope': 'frozen 2026-09-23 source before later fixes; synthetic graph, not latest-source or OCR/GPU qualification',
                'prior_interruption': 'stability-02-interruption.json'}},
            'browser_qa': 'browser-qa-15/verification.json (synthetic controller); gpu-ui-03/verification.json (20-page real local GPU batch playback after scoped API)',
            'candidate_package': package_receipt,
            'candidate_source_matches_current': source_match,
            'candidate_note': ('Candidate 02 source matches current files; package audit and archive remain separate gates' if source_match else
                               'Candidate 01 is immutable and has a source archive omission; current source still requires candidate 02 verification'),
            'mixed_stability': {'status': 'smoke_only_investigation_needed', 'source': 'mixed-smoke-06/performance-assessment.json',
                                'note': 'Functional smoke passed but single-sample GPU throughput and API p95 crossed investigation thresholds; four-hour final-source run not yet qualified'},
            'prior_full_backend_failure': {'log': 'full-backend-final-20260923.log', 'result': '635 tests, 11 fixture failures after context graph change; fixed maintenance fixture; full rerun required'}}
    (AUDIT / 'latest-development-checks.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    state = json.loads((AUDIT / 'task-state.json').read_text('utf-8'))
    for task in state['tasks']:
        if task['id'] in {'C01', 'C03', 'D02', 'D03', 'D04', 'D05', 'D06', 'F01', 'F02', 'E04', 'G01'}:
            task['status'] = 'in_progress'
            task['evidence'] = list(dict.fromkeys(task.get('evidence', []) + ['latest-development-checks.json', 'agent-latest-regression.log', 'NEXT.md']))
    state['updated'] = data['updated']
    (AUDIT / 'task-state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    baseline = json.loads((AUDIT / 'baseline.json').read_text('utf-8'))
    frozen = {p: h for p, h in baseline['files'].items() if p.startswith('audit/ocr-agent-20260920-p0/') or p in {
        'src/ocr_workbench/agent/contracts.py', 'docs/ocr-agent-development-plan-20260920.md',
        'docs/ocr-agent-development-tasks-20260920.json', 'docs/ocr-agent-acceptance-plan-20260920.md'}}
    changed = [p for p, h in frozen.items() if not (ROOT / p).exists() or hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h]
    (AUDIT / 'frozen-preservation.json').write_text(json.dumps({'status': 'fail' if changed else 'pass', 'checked_files': len(frozen), 'changed': changed}, indent=2) + '\n', encoding='utf-8')
    if changed or any(c['status'] != 'pass' for c in checks.values()):
        raise SystemExit('Development evidence contains failures')
    print(f'Recorded {len(checks)} completed checks; {len(frozen)} frozen files unchanged. Phase remains in progress.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    record()
