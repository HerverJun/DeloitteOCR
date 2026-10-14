"""Pair local audit API timestamps and separate transport from route latency.

This is a diagnostic for mixed_stability.py --trace-api-spans. It does not
replace the F06 four-hour functional or performance qualification receipt.
"""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics


EVENTS = ('client_start', 'server_entry', 'server_exit', 'client_end')
PARTS = ('pre_service', 'server_route', 'post_service', 'total')


def distribution(values):
    ordered = sorted(values)
    return {
        'samples': len(ordered),
        'median_ms': statistics.median(ordered),
        'p95_ms': ordered[math.ceil(len(ordered) * .95) - 1],
        'max_ms': ordered[-1],
        'over_1000ms': sum(value > 1000 for value in ordered),
    }


def assess(path):
    requests = defaultdict(dict)
    rows = 0
    invalid = []
    for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        rows += 1
        try:
            event = json.loads(line)
            request_id = event['request_id']
            name = event['event']
            timestamp = event['perf_ns']
            if not isinstance(request_id, str) or not request_id or not isinstance(name, str) or not name \
                    or not isinstance(timestamp, int):
                raise ValueError('invalid request ID, event name or timestamp')
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
            invalid.append({'line': line_number, 'reason': 'malformed_event', 'detail': str(error)})
            continue
        if name in requests[request_id]:
            invalid.append({'request_id': request_id, 'reason': 'duplicate_event', 'event': name})
        else:
            requests[request_id][name] = event

    groups = defaultdict(lambda: defaultdict(list))
    pair_map = defaultdict(list)
    valid = 0
    for request_id, events in requests.items():
        if set(events) != set(EVENTS):
            invalid.append({'request_id': request_id, 'reason': 'incomplete_request',
                            'events': sorted(events)})
            continue
        markers = [events[name] for name in EVENTS]
        if any(not isinstance(event.get('route'), str) or not event['route']
               or not isinstance(event.get('phase'), str) or not event['phase'] for event in markers) \
                or len({event['route'] for event in markers}) != 1 \
                or len({event['phase'] for event in markers}) != 1:
            invalid.append({'request_id': request_id, 'reason': 'route_or_phase_mismatch'})
            continue
        times = [event['perf_ns'] for event in markers]
        if times != sorted(times):
            invalid.append({'request_id': request_id, 'reason': 'time_order_error', 'times_ns': times})
            continue
        start, entry, exit_, end = times
        durations = {'pre_service': (entry - start) / 1_000_000,
                     'server_route': (exit_ - entry) / 1_000_000,
                     'post_service': (end - exit_) / 1_000_000,
                     'total': (end - start) / 1_000_000}
        label = (markers[0]['phase'], markers[0]['route'], markers[0].get('transport', 'unknown'))
        for part, milliseconds in durations.items():
            groups[label][part].append(milliseconds)
        if markers[0].get('pair_id') is not None:
            pair_map[markers[0]['pair_id']].append((label, durations, request_id))
        valid += 1

    summary = []
    for (phase, route, transport), parts in sorted(groups.items()):
        summary.append({'phase': phase, 'route': route, 'transport': transport,
                        'latency_ms': {part: distribution(parts[part]) for part in PARTS}})
    pairs = []
    for pair_id, entries in sorted(pair_map.items()):
        by_transport = {label[2]: (label, durations, request_id) for label, durations, request_id in entries}
        if len(entries) != 2 or set(by_transport) != {'per-request', 'persistent'} \
                or len({label[:2] for label, _, _ in entries}) != 1:
            invalid.append({'pair_id': pair_id, 'reason': 'incomplete_or_mismatched_transport_pair'})
            continue
        fresh = by_transport['per-request']
        kept = by_transport['persistent']
        pairs.append({'pair_id': pair_id, 'phase': fresh[0][0], 'route': fresh[0][1],
                      'request_ids': {'per-request': fresh[2], 'persistent': kept[2]},
                      'per_request_ms': fresh[1], 'persistent_ms': kept[1],
                      'fresh_minus_persistent_ms': {part: fresh[1][part] - kept[1][part] for part in PARTS}})
    pair_groups = []
    for phase, route in sorted({(item['phase'], item['route']) for item in pairs}):
        members = [item for item in pairs if (item['phase'], item['route']) == (phase, route)]
        pair_groups.append({'phase': phase, 'route': route, 'pairs': len(members),
                            'fresh_minus_persistent_ms': {part: distribution([item['fresh_minus_persistent_ms'][part]
                                                        for item in members]) for part in PARTS},
                            'fresh_slower_count': sum(item['fresh_minus_persistent_ms']['total'] > 0 for item in members)})
    return {'status': 'pass' if not invalid and valid else 'invalid_spans',
            'checked_utc': datetime.now(timezone.utc).isoformat(),
            'source': str(path.resolve()), 'event_rows': rows, 'requests': len(requests),
            'valid_requests': valid, 'invalid': invalid, 'groups': summary,
            'transport_pairs': pairs, 'pair_groups': pair_groups,
            'limits': ['Client and server timestamps share one worker monotonic clock.',
                       'Pre-service includes client setup, connection and server admission; post-service includes response reading and client cleanup.',
                       'A paired route span does not isolate SQLite lock wait or model time.',
                       'The diagnostic does not alter or qualify the original complete-loopback F06 metric.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spans', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = assess(args.spans)
    content = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(content, encoding='utf-8')
    else:
        print(content, end='')
    if result['status'] != 'pass':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
