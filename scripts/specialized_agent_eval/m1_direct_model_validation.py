"""Run a frozen screen batch through the real native queue without a controller.

This supplies M1 model/content evidence when the Agent provider is unavailable.
It never reads reference truth or the original native SQLite database. M3 still
requires a separate real Agent chain on final frozen source.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import zipfile

from common import digest, fpath, frozen_json, read_json
from freeze_sources import PARTS, inventory


def observed_time():
    return datetime.now(timezone.utc).isoformat()


def run(args):
    source, inputs, output, bundle, pdf_layer = map(fpath,
        (args.source, args.inputs, args.output, args.bundle, args.pdf_layer))
    if output.exists() or inputs.parent.name not in {'development', 'validation'}:
        raise ValueError('New development/validation identity required')
    trial = read_json(source / 'trial-source-binding.json')
    if (fpath(trial['actual_output']) != output or trial['inputs']['sha256'] != digest(inputs)
            or trial['evaluation_split'] != inputs.parent.name):
        raise ValueError('Frozen trial does not bind this exact output and split')
    platform = source / 'platform'
    harness = platform / 'scripts/evaluate_screen_agent_native.py'
    sys.path[:0] = [str(platform), str(platform / 'src'), str(args.agent_layer)]
    spec = importlib.util.spec_from_file_location('m1_frozen_direct_native', harness)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actual_args = SimpleNamespace(output=output, inputs=inputs, native_source=source / 'ocr',
        bundle=bundle, agent_layer=fpath(args.agent_layer), pdf_layer=pdf_layer,
        context_manifest=None, run=True)
    root = module.configure(actual_args)
    mapping, prepared = module.prepare(actual_args, root)
    if (prepared['screen_policy'] != trial['policy'] or len(mapping) != trial['scope_inputs']
            or any(inventory(source / family, PARTS[family]) != trial['sources'][family]['files']
                   for family in ('ocr', 'platform', 'classification'))
            or module.inventory(source / 'ocr') != prepared['native_source_files']
            or module.platform_inventory() != read_json(root / 'platform-source-binding.json')):
        raise ValueError('Native preparation differs from frozen trial')
    from scripts.evaluate_screen_agent_replay import services
    from workbench_platform.jobs.screen_batches import ScreenSubmit, ScreenPolicy, ScreenExport
    token = secrets.token_urlsafe(36)
    (root / 'native-token.txt').write_text(token, encoding='utf-8')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    command = [str(bundle / 'runtimes/control/python.exe'), '-I', '-B', '-X', 'utf8',
        str(harness), '--serve-native', '--output', str(root), '--inputs', str(inputs),
        '--native-source', str(source / 'ocr'), '--bundle', str(bundle),
        '--pdf-layer', str(pdf_layer), '--port', str(port)]
    service = SimpleNamespace(base_url=f'http://127.0.0.1:{port}', token=token, asset_map={})
    child, keeper, failure, result = None, None, None, None
    started = time.perf_counter()
    events = []
    def mark(stage, **extra):
        events.append({'stage': stage, 'utc': observed_time(),
                       'elapsed_seconds': time.perf_counter() - started, **extra})
    with (root / 'native-service.log').open('wb') as log:
        try:
            mark('native_start')
            child = subprocess.Popen(command, cwd=root, env=os.environ.copy(),
                stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.perf_counter() + 90
            while time.perf_counter() < deadline:
                if child.poll() is not None:
                    raise RuntimeError('Owned native service exited before readiness')
                try:
                    health = module.native_http(service, 'GET', '/api/health')
                    if health.get('status') == 'ready' and health.get('queue', {}).get('healthy'):
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(.5)
            else:
                raise RuntimeError('Owned native service readiness deadline exceeded')
            mark('native_ready')
            project = module.native_http(service, 'POST', '/api/projects',
                {'name': 'Isolated direct model validation', 'request_id': args.identity + '_Project'})
            service.project_id = project['id']
            value, fixed, link, keeper = services(root, mapping, service)
            mark('platform_inputs_registered', count=len(mapping))
            submission = ScreenSubmit(link_id=link.link_id, asset_set_id=fixed.asset_set_id,
                asset_set_revision=fixed.revision, request_id=args.identity + '_Batch',
                policy=ScreenPolicy())
            first = value.screen_batches.submit('wreplay', submission)
            batch_id = first['batch_id']
            mark('native_batch_submitted', batch_id=batch_id)
            first_result = None
            deadline = time.perf_counter() + args.timeout
            status = first
            while True:
                counts = status['counts']
                if first_result is None and counts['completed'] + counts['needs_review'] > 0:
                    first_result = {'utc': observed_time(),
                        'elapsed_seconds': time.perf_counter() - started,
                        'count': counts['completed'] + counts['needs_review']}
                    mark('first_native_result', count=first_result['count'])
                if counts['pending'] == 0:
                    break
                if time.perf_counter() >= deadline:
                    raise TimeoutError('Direct native validation deadline reached with pending items')
                time.sleep(args.poll_seconds)
                status = value.screen_batches.status('wreplay', batch_id, limit=1)
            mark('native_batch_terminal', counts=status['counts'])
            receipts = root / 'native-receipts'
            receipts.mkdir()
            by_asset = {item['asset_id']: item for item in mapping}
            records = []
            for offset in range(0, len(mapping), 30):
                page = value.screen_batches.status('wreplay', batch_id, offset=offset, limit=30)
                for item in page['items']:
                    sample = by_asset[item['asset_id']]
                    detail = value.screen_batches.detail('wreplay', batch_id, item['input_version_id'])
                    detail.update(sample_id=sample['sample_id'],
                                  source_image_sha256=sample['source_image_sha256'])
                    frozen_json(receipts / (sample['sample_id'] + '.json'), detail)
                    effective = detail.get('result') or {}
                    original = effective.get('original') or {}
                    quality = original.get('quality') or {}
                    records.append({'id': sample['sample_id'],
                        'image_sha256': sample['source_image_sha256'],
                        'input_version_id': item['input_version_id'],
                        'result_id': effective.get('id'), 'result_revision': effective.get('revision'),
                        'status': detail['status'], 'review_state': detail['review_state'],
                        'human_confirmed': detail.get('human_confirmed', False),
                        'candidate': {'original': original, 'edited': effective.get('edited') or {}},
                        'review_image': detail['status'] == 'needs_review', 'review_regions': [],
                        'automatic_delivery': bool(quality.get('automatic_delivery')),
                        'issues': detail.get('issues') or []})
            if len(records) != len(mapping) or len({row['id'] for row in records}) != len(mapping):
                raise ValueError('Native detail readback lacks a unique full denominator')
            records.sort(key=lambda row: row['id'])
            observations = {'format': 'specialized-agent-eval-v1',
                'evidence_kind': 'native_direct_platform_receipts', 'inputs_sha256': digest(inputs),
                'records': records, 'policy': status['policy'],
                'scope': 'Actual direct native queue/model and platform details; no Agent or truth'}
            frozen_json(root / 'observations.json', observations)
            export = None
            if status['counts']['completed'] + status['counts']['needs_review']:
                before = value.screen_batches.delivery_snapshot('wreplay', batch_id)
                package = value.screen_batches.export('wreplay', batch_id,
                    ScreenExport(include_pending_review=True))
                artifact = value.artifact_center.file('wreplay', package['bundle_id'], 'ocr-results.zip')
                with value.artifact_center.store.stage_download('wreplay', artifact.artifact_id,
                        expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
                    target = root / 'delivered.zip'
                    shutil.copyfile(path, target)
                if digest(target) != artifact.blob_hash:
                    raise ValueError('Published ZIP hash differs from downloaded bytes')
                with zipfile.ZipFile(target) as archive:
                    manifest = json.loads(archive.read('manifest.json'))
                    files = [name for name in archive.namelist() if name.endswith('.xlsx')]
                after = value.screen_batches.delivery_snapshot('wreplay', batch_id)
                export = {'bundle_id': package['bundle_id'], 'zip_sha256': digest(target),
                    'xlsx_files': len(files), 'manifest_items': len(manifest['items']),
                    'manifest_success_files': manifest['success_files'],
                    'snapshot_before': before, 'snapshot_after': after}
                frozen_json(root / 'export-receipt.json', export)
                mark('platform_export_downloaded', xlsx_files=len(files))
            result = {'format': 'm1-direct-native-model-validation-v1', 'status': 'terminal',
                'evaluation_split': trial['evaluation_split'], 'scope_inputs': len(mapping),
                'batch_id': batch_id, 'counts': status['counts'], 'policy': status['policy'],
                'first_native_result': first_result, 'wall_seconds': time.perf_counter() - started,
                'source_binding_sha256': digest(source / 'trial-source-binding.json'),
                'prepared_sha256': digest(root / 'prepared.json'),
                'observations_sha256': digest(root / 'observations.json'),
                'export': export, 'truth_read': False, 'agent_calls': 0,
                'real_native_queue_and_model': True, 'final_agent_qualification': False,
                'same_source_performance_comparison': False}
        except Exception as error:
            failure = error
            mark('execution_failed', error_type=type(error).__name__, error=str(error))
        finally:
            if keeper is not None:
                keeper.close()
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=15)
            mark('native_stopped', child_exit_code=child.returncode if child else None)
            frozen_json(root / 'timeline.json', {'events': events})
            if result is None:
                result = {'format': 'm1-direct-native-model-validation-v1', 'status': 'failed',
                    'scope_inputs': len(mapping), 'source_binding_sha256': digest(source / 'trial-source-binding.json'),
                    'prepared_sha256': digest(root / 'prepared.json'), 'truth_read': False,
                    'agent_calls': 0, 'error_type': type(failure).__name__ if failure else 'unknown',
                    'error': str(failure) if failure else 'unknown',
                    'final_agent_qualification': False}
            result['timeline_sha256'] = digest(root / 'timeline.json')
            frozen_json(root / 'result.json', result)
    if failure is not None:
        raise failure
    print({'status': result['status'], 'inputs': result['scope_inputs'],
           'counts': result['counts'], 'xlsx': result['export']['xlsx_files'] if result['export'] else 0})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'inputs', 'output', 'bundle', 'agent-layer', 'pdf-layer', 'identity'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--timeout', type=float, default=14400)
    parser.add_argument('--poll-seconds', type=float, default=5)
    run(parser.parse_args())
