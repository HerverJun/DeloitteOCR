"""Assess a mixed-stability receipt against the acceptance-plan engineering targets.

This is an analysis of recorded local measurements, not a replacement for the
functional receipt, target-machine qualification, or investigation of outliers.
"""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics


API_LIMIT_MS = 1000
PROGRESS_LIMIT_MS = 2000
THROUGHPUT_INVESTIGATE_RATIO = 0.95


def assess(folder):
    receipt = json.loads((folder / 'mixed-stability.json').read_text(encoding='utf-8'))
    ui_path = folder / 'ui-result.json'
    ui = json.loads(ui_path.read_text(encoding='utf-8')) if ui_path.exists() else None
    metrics = receipt.get('metrics', {})
    api = {key: {'samples': data['samples'], 'p95_ms': data['p95_ms'],
                 'over_1000ms': data['p95_ms'] > API_LIMIT_MS}
           for key, data in metrics.items()
           if not key.startswith('service:') and key.rsplit(':', 1)[-1] in {'api', 'state_api', 'progress_api', 'cancel_api'}}
    progress = {key: {'samples': data['samples'], 'p95_ms': data['p95_ms'],
                      'over_2000ms': data['p95_ms'] > PROGRESS_LIMIT_MS}
                for key, data in metrics.items() if key.endswith(':ui_progress')}
    required_progress = ['mixed_two_ui:ui_progress', 'soak:ui_progress'] if receipt.get('service_mode') == 'independent_process' else []
    missing_progress = [key for key in required_progress if progress.get(key, {}).get('samples', 0) <= 0]
    cancellation = {key: {'samples': data['samples'], 'p95_ms': data['p95_ms'],
                          'over_1000ms': data['p95_ms'] > API_LIMIT_MS}
                    for key, data in metrics.items() if key.endswith(':cancel_api')}
    phases = defaultdict(list)
    for row in receipt.get('comparisons', []):
        phases[row['phase']].append(row['pages_per_second'])
    baseline = phases.get('agent_off', [])
    throughput = {}
    for phase, samples in phases.items():
        paired = [samples[i] / baseline[i] for i in range(min(len(samples), len(baseline)))] if baseline else []
        mean_ratio = statistics.mean(samples) / statistics.mean(baseline) if baseline else None
        throughput[phase] = {
            'pages_per_second': samples, 'mean_pages_per_second': statistics.mean(samples),
            'stdev_pages_per_second': statistics.stdev(samples) if len(samples) > 1 else None,
            'paired_ratios': paired, 'mean_ratio_to_off': mean_ratio,
            'investigate_over_5pct_drop': mean_ratio < THROUGHPUT_INVESTIGATE_RATIO if mean_ratio is not None else None,
        }

    samples = []
    resources_path = folder / 'resources.jsonl'
    if resources_path.exists():
        for line in resources_path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                if row['phase'] == 'soak':
                    samples.append(row)
    resources = {'soak_samples': len(samples), 'first_last_15min': None}
    if samples:
        first = datetime.fromisoformat(samples[0]['utc']).timestamp()
        last = datetime.fromisoformat(samples[-1]['utc']).timestamp()
        window = min(900, (last - first) / 3)
        early = [r for r in samples if datetime.fromisoformat(r['utc']).timestamp() <= first + window]
        late = [r for r in samples if datetime.fromisoformat(r['utc']).timestamp() >= last - window]
        def median(rowset, field):
            return statistics.median(r[field] for r in rowset)
        resources['first_last_15min'] = {
            'window_seconds': window, 'early_samples': len(early), 'late_samples': len(late),
            'span_seconds': last - first,
            'tree_rss_bytes': {'early_median': median(early, 'tree_rss_bytes'), 'late_median': median(late, 'tree_rss_bytes')},
            'tree_handles': {'early_median': median(early, 'tree_handles'), 'late_median': median(late, 'tree_handles')},
            'tree_threads': {'early_median': median(early, 'tree_threads'), 'late_median': median(late, 'tree_threads')},
            'children': {'early_median': median(early, 'children'), 'late_median': median(late, 'children')},
        }
        for field in ['tree_rss_bytes', 'tree_handles', 'tree_threads', 'children']:
            entry = resources['first_last_15min'][field]
            entry['change'] = entry['late_median'] - entry['early_median']

    counts = receipt.get('run_counts', {})
    ui_clients = (ui or {}).get('clients', [])
    functional = receipt.get('status') == 'pass' and not missing_progress and (ui or {}).get('passed') is True and len(ui_clients) == 2 \
        and all(c['cycles'] > 0 and c['streams'] > 0 and not c['errors'] for c in ui_clients) \
        and all(counts.get(k, 0) > 0 for k in ['READ', 'ASK', 'CANCEL', 'OCR', 'visual']) \
        and receipt.get('shutdown', {}).get('service_closed') is True \
        and receipt.get('shutdown', {}).get('ui_returncode') == 0
    return {
        'source_receipt': str(folder / 'mixed-stability.json'),
        'functional_pass': functional,
        'requested_mixed_seconds': receipt.get('requested_mixed_seconds'),
        'elapsed_mixed_seconds': receipt.get('elapsed_mixed_seconds'),
        'api_dispatch_p95': api, 'visible_progress_p95': progress,
        'cancellation_api_p95': cancellation,
        'model_wait_no_extra_requests': counts.get('OCR', 0) > 0 and functional,
        'throughput': throughput, 'resource_curve': resources,
        'performance_findings': {
            'api_over_1s': [key for key, value in api.items() if value['over_1000ms']],
            'progress_over_2s': [key for key, value in progress.items() if value['over_2000ms']],
            'cancel_over_1s': [key for key, value in cancellation.items() if value['over_1000ms']],
            'throughput_drop_over_5pct': [key for key, value in throughput.items() if value['investigate_over_5pct_drop']],
            'missing_soak_resource_samples': len(samples) == 0,
            'missing_ui_progress_samples': missing_progress,
        },
        'limits': [
            'The API histogram records complete loopback requests, including client and test-harness overhead; it is not an isolated server dispatch measurement.',
            'Cancellation timing is the HTTP response time and immediate cancelled status, not independent observation of all downstream effects.',
            'Visible progress is measured from persisted completion event to UI receipt and includes browser polling.',
            'Throughput ratios compare warmed GPU processing intervals; each batch has an untimed first task, and ordering/drift can affect small samples.',
            'Resource start/end changes alone cannot diagnose a leak; inspect the full resource curve and expected SQLite growth.',
            'The controller is synthetic; visual calls use a local HTTP fixture. This does not qualify cloud-provider latency or semantic quality.',
        ],
    }


def publish(folder, destination):
    """Write named F06 receipts without treating an in-progress run as qualified."""
    analysis = assess(folder)
    raw = json.loads((folder / 'mixed-stability.json').read_text(encoding='utf-8'))
    repeats_complete = all(len(analysis['throughput'].get(phase, {}).get('pages_per_second', [])) >= 3
                           for phase in ['agent_off', 'agent_idle', 'agent_read', 'agent_ocr', 'mixed_two_ui'])
    duration_complete = raw.get('requested_mixed_seconds', 0) >= 14400 \
        and raw.get('elapsed_mixed_seconds', 0) >= 14400
    qualified = repeats_complete and duration_complete and analysis['functional_pass']
    findings = analysis['performance_findings']
    perf_exceeded = any(findings[key] for key in ['api_over_1s', 'progress_over_2s', 'cancel_over_1s',
                                                 'throughput_drop_over_5pct'])
    common = {
        'updated_utc': datetime.now(timezone.utc).isoformat(),
        'source_receipt': analysis['source_receipt'],
        'frozen_source_sha256': raw.get('source_sha256'),
        'asset_manifest_sha256': raw.get('asset_manifest_sha256'),
        'hardware': raw.get('hardware'),
        'qualification': {'three_repeats_complete': repeats_complete,
                          'four_hour_mixed_complete': duration_complete,
                          'functional_pass': analysis['functional_pass']},
    }
    performance = {**common, 'area': 'F06 performance',
                   'status': ('not_qualified' if not qualified else
                              'investigation_needed' if perf_exceeded else 'qualified_pass'),
                   'thresholds': {'api_dispatch_p95_ms': 1000, 'visible_progress_p95_ms': 2000,
                                  'cancel_control_p95_ms': 1000, 'extra_model_requests_while_waiting': 0,
                                  'throughput_decrease_investigate_over_fraction': 0.05},
                   'assessment': analysis}
    stability = {**common, 'area': 'F06/E61 mixed stability',
                 'status': 'qualified_pass' if qualified and not findings['missing_soak_resource_samples'] else 'not_qualified',
                 'requested_mixed_seconds': raw.get('requested_mixed_seconds'),
                 'elapsed_mixed_seconds': raw.get('elapsed_mixed_seconds'),
                 'run_counts': raw.get('run_counts'), 'ui_clients': raw.get('ui_clients'),
                 'visual_requests_count': raw.get('visual_requests_count'),
                 'errors': raw.get('errors'), 'shutdown': raw.get('shutdown'),
                 'resource_curve': analysis['resource_curve'],
                 'limits': analysis['limits']}
    destination.mkdir(parents=True, exist_ok=True)
    for name, value in [('performance.json', performance), ('stability.json', stability)]:
        path = destination / name
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    return performance, stability


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path, help='Directory containing mixed-stability.json')
    parser.add_argument('--output', type=Path, help='Optional analysis JSON path')
    parser.add_argument('--publish-to', type=Path, help='Write named performance.json and stability.json in this directory')
    args = parser.parse_args()
    analysis = assess(args.folder.resolve())
    content = json.dumps(analysis, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(content, encoding='utf-8')
    if args.publish_to:
        publish(args.folder.resolve(), args.publish_to.resolve())
    print(content, end='')


if __name__ == '__main__':
    main()
