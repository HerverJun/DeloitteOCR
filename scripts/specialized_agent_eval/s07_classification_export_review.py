"""CPU review of actual classification exports, source/model bytes and labels."""
from __future__ import annotations
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import sys
from common import digest, fingerprint, fpath, frozen_bytes, frozen_json, read_json
from s07_native_read import cold_copy, inventory
from s07_agent_native_followup import platform_binding
from s07_native_export_review import rows


def review(args):
    original, source, root = map(fpath, (args.original_run, args.platform_source, args.output_root))
    if root.exists():
        raise ValueError('Classification export review needs a new immutable F: root')
    before, bound = inventory(original), platform_binding(source)
    prepared, goal, result = [read_json(original / name) for name in ('prepared.json', 'actual-goal.json', 'result.json')]
    native = fpath(prepared['owned_native']); source_workspace = fpath(prepared['original_workspace'])
    source_before = inventory(source_workspace)
    if source_before != prepared['original_workspace_files']:
        raise ValueError('Original model/input/label workspace differs from actual preparation')
    root.mkdir(parents=True); temp = root / 'temp'; temp.mkdir()
    for name in ('TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME'):
        os.environ[name] = str(temp)
    sys.path[:0] = [str(source), str(source / 'src')]
    from workbench_platform.artifacts.store import ArtifactStore
    copied = {}
    for label, database in (('native', native / 'platform.sqlite3'),
                            ('agent', original / 'platform/agent/operations.sqlite3'),
                            ('audit', original / 'platform/audit/audit.sqlite3')):
        copied[label] = cold_copy(database, root / (label + '.sqlite3'))
    frozen_json(root / 'original-database-copy-qualification.json', copied)
    operations = read_json(original / 'actual-agent-operations.json')
    literal = rows(root / 'agent.sqlite3', 'SELECT * FROM operations WHERE run_id=? ORDER BY created', (args.run_id,))
    if len(operations) != len(literal) or any(expected['id'] != actual['id']
            or expected['arguments'] != json.loads(actual['arguments'])
            or expected['receipt'] != json.loads(actual['result']) for expected, actual in zip(operations, literal, strict=True)):
        raise ValueError('Actual literal operation ledger differs from original byte copy')
    success = {entry['tool']: entry for entry in operations if (entry['receipt'] or {}).get('status') == 'success'}
    infer = success['classification_infer']; read = success['classification_read_results']['receipt']
    if infer['arguments']['device'] != 'cpu' or infer['arguments']['model_id'] != prepared['model_id']:
        raise ValueError('Actual inference changed the explicit CPU/published-model contract')
    bundle = success['classification_export']['receipt']['data']['bundle']
    artifacts = root / 'artifacts'; artifacts.mkdir()
    cold_copy(original / 'platform/artifacts/artifacts.sqlite3', artifacts / 'artifacts.sqlite3')
    shutil.copytree(original / 'platform/artifacts/blobs', artifacts / 'blobs')
    store = ArtifactStore(artifacts / 'artifacts.sqlite3', artifacts / 'blobs')
    files = {}
    for item in bundle['files']:
        artifact = store.get(bundle['workspace_id'], item['artifact_id'])
        with store.stage_download(bundle['workspace_id'], artifact.artifact_id,
                expected_revision=artifact.revision, expected_hash=artifact.blob_hash) as path:
            target = root / 'delivered' / item['name']; frozen_bytes(target, path.read_bytes())
        if digest(target) != artifact.blob_hash or target.stat().st_size != item['size_bytes']:
            raise ValueError('Exported artifact bytes or exact size differ')
        files[item['name']] = {'sha256': digest(target), 'size_bytes': target.stat().st_size, 'artifact_id': artifact.artifact_id}
    exported, provenance = [read_json(root / 'delivered' / name) for name in ('classification-results.json', 'provenance.json')]
    with (root / 'delivered/classification-results.csv').open(encoding='utf-8-sig', newline='') as stream:
        csv_rows = list(csv.DictReader(stream))
    native_result_refs = [ref for ref in read['result_refs']
                          if ref.get('kind') == 'result' and ref.get('application') == 'classification']
    if (exported['model_id'] != prepared['model_id'] or provenance['model_id'] != prepared['model_id']
            or exported['counts'] != {'cancelled': 0, 'failed': 0, 'pending': 0, 'succeeded': 2, 'total': 2}
            or set(goal['selected_assets']) != {item['asset_id'] for item in exported['items']}
            or len(csv_rows) != 2 or len(native_result_refs) != 1
            or exported['native_job_id'] != native_result_refs[0]['native_id']
            or read['data']['handoff_id'] != infer['receipt']['data']['handoff_id']):
        raise ValueError('Export model or explicit selected-input/native-result coverage changed')
    actual_items = {item['asset_id']: item for item in read['data']['items']}
    csv_items = {item['asset_id']: item for item in csv_rows}
    for item in exported['items']:
        if (any(actual_items[item['asset_id']].get(key) != value
                for key, value in item.items() if key != 'effective_label_id')
                or item['effective_label_id'] != item['prediction_label_id']):
            raise ValueError('JSON changed an actual prediction, confidence, status or label')
        csv_item = csv_items[item['asset_id']]
        for key, value in csv_item.items():
            expected = item.get(key)
            if value != ('' if expected is None else str(expected)):
                raise ValueError('CSV differs from the actual JSON prediction or label')
        if (item['prediction_status'] != 'needs_review' or item['human_label_id'] is not None
                or item['current_annotation_label_id'] is not None or item['disposition_state'] != 'unreviewed'
                or item['disposition_revision'] != 0 or not 0 <= item['prediction_score'] < .6):
            raise ValueError('Low-confidence historical engineering prediction was accepted or relabeled')
    if ({item['asset_id'] for item in provenance['inputs']} != set(goal['selected_assets'])
            or {item['blob_hash'] for item in provenance['inputs']} != {item['image_sha256'] for item in prepared['inputs']}):
        raise ValueError('Export source provenance expanded or substituted the explicit images')
    annotations = rows(root / 'native.sqlite3', 'SELECT * FROM annotations ORDER BY id')
    if fingerprint(annotations) != prepared['annotations_sha256']:
        raise ValueError('Owned native inference changed historical human labels')
    models = []
    for relocation in prepared['database_file_relocations']:
        if relocation['table'] != 'models':
            continue
        path = fpath(relocation['owned_copy'])
        actual_sha = fingerprint(inventory(path)) if path.is_dir() else digest(path)
        if actual_sha != relocation['sha256']:
            raise ValueError('Owned published-model package changed during inference')
        models.append({'model_id': relocation['id'], 'sha256': actual_sha})
    evidence = success['audit_read_evidence']['receipt']['data']
    stored = json.loads(rows(root / 'audit.sqlite3', 'SELECT body FROM direct_audit_evidence WHERE evidence_id=?', (evidence['evidence_id'],))[0]['body'])
    if evidence['stale'] is not False or stored['snapshot_hash'] != evidence['snapshot_hash'] or stored['reference'] not in read['result_refs']:
        raise ValueError('Classification evidence differs from the actual model result')
    delivery = success['delivery_publish']['receipt']
    if (delivery['data']['verified'] is not True or bundle['bundle_id'] not in {item['bundle_id'] for item in delivery['data']['delivery']}
            or not all(ref in delivery['result_refs'] for ref in read['result_refs'])
            or not any(ref['kind'] == 'evidence' and ref['sha256'] == evidence['snapshot_hash'] for ref in delivery['result_refs'])):
        raise ValueError('Delivery omitted the actual classification result or direct evidence')
    status = rows(root / 'agent.sqlite3', 'SELECT status FROM runs WHERE id=?', (args.run_id,))[0]['status']
    if status == 'succeeded' and delivery['data']['completeness'] != 'complete':
        raise ValueError('Succeeded classification run lacks complete verified delivery')
    if (status != result['agent_status'] or inventory(original) != before or inventory(source_workspace) != source_before
            or platform_binding(source) != bound):
        raise ValueError('Original run/source workspace/status or frozen source changed')
    frozen_json(root / 'export-review.json', {'format': 's07-classification-export-independent-review-v1', 'status': 'pass',
        'original_run': str(original), 'run_id': args.run_id, 'original_agent_status': status,
        'original_run_unchanged': True, 'original_workspace_unchanged': True, 'terminal_status_rewritten': False,
        'source_files': bound, 'artifacts': files, 'published_model_packages': models,
        'human_annotation_count': len(annotations), 'human_annotation_sha256': fingerprint(annotations),
        'selected_source_count': 2, 'predictions': exported['items'], 'delivery_completeness': delivery['data']['completeness'],
        'delivery_limitations': delivery['data']['limitations'], 'evidence_snapshot_hash': evidence['snapshot_hash'],
        'controller_calls': 0, 'gpu_calls': 0, 'training_calls': 0, 'helper_sha256': digest(__file__),
        'scope': 'Production artifact download on an independent byte copy; exact exported actual predictions, historical labels/source/model bytes and preserved actual terminal status; two synthetic engineering inputs, no business accuracy'})
    print('classification_export_review_pass', status, len(files), len(exported['items']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('original-run', 'platform-source', 'output-root', 'run-id'):
        parser.add_argument('--' + name, required=True)
    review(parser.parse_args())
