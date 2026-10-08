"""Qualify an existing registered published package; explicit CPU execution only.

This is a real published-model primitive example, not a controller/native tool
integration claim. No training, threshold changes or annotation writes occur.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import importlib
import importlib.metadata
import json
import sqlite3
import sys
import time
from common import FORMAT, digest, fingerprint, fpath, frozen_bytes, frozen_json, read_json


def source_files(source):
    files = {}
    for folder in ('src', 'schemas'):
        for path in sorted((source / folder).rglob('*')):
            if path.is_symlink() or path.is_junction():
                raise ValueError('Classification source contains a linked file')
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                files[path.relative_to(source).as_posix()] = digest(path)
    return files


def readonly_database(path):
    # A quiescent restored database can be read immutable, without creating
    # SQLite WAL/SHM in the source. Do not silently ignore committed WAL.
    wal = path.with_name(path.name + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Source classification database has WAL; obtain a qualified cold copy first')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def one(db, table, identity):
    rows = [dict(row) for row in db.execute('SELECT * FROM "' + table + '" WHERE id=?', (identity,))]
    if len(rows) != 1:
        raise ValueError('Missing/ambiguous actual classification ' + table + ' identity')
    return rows[0]


def runtime_version_evidence(distribution, module_name):
    module = importlib.import_module(module_name)
    path = fpath(module.__file__)
    evidence = {'module': module_name, 'module_file': str(path), 'module_file_sha256': digest(path),
                'module_version': getattr(module, '__version__', None)}
    try:
        evidence.update(version=importlib.metadata.version(distribution), version_source='distribution_metadata')
    except importlib.metadata.PackageNotFoundError:
        evidence.update(version=evidence['module_version'], version_source='loaded_module.__version__',
                        distribution_metadata_available=False)
    if not isinstance(evidence['version'], str) or not evidence['version']:
        raise ValueError('Loaded runtime module has no inspectable version: ' + module_name)
    return evidence


def prepare(source, workspace, model_id, output):
    source, workspace, output = map(fpath, (source, workspace, output))
    source_manifest = source_files(source)
    sys.path.insert(0, str(source / 'src'))
    from image_platform.runtime import validate_package
    from image_platform.settings import REPO
    if fpath(REPO) != source:
        raise ValueError('Classification package validator imported another source')
    database = workspace / 'platform.sqlite3'
    db_sha = digest(database)
    with closing(readonly_database(database)) as db:
        model = one(db, 'models', model_id)
        project = one(db, 'projects', model['project_id'])
        if project['active_model_id'] != model_id:
            raise ValueError('Selected package is not the project registered active published model')
        package = fpath(model['package_path'])
        if not package.is_relative_to(workspace / 'models'):
            raise ValueError('Registered model package escaped the read-only source workspace')
        metadata = validate_package(package)
        if (metadata != json.loads(model['metadata']) or metadata['model_version_id'] != model_id
                or metadata['project_id'] != project['id']):
            raise ValueError('Published package metadata differs from actual model registration')
        dataset = one(db, 'datasets', metadata['provenance']['dataset_version_id'])
        manifest_path = fpath(dataset['manifest_path'])
        if not manifest_path.is_relative_to(workspace / 'datasets') or digest(manifest_path) != dataset['manifest_sha256']:
            raise ValueError('Registered restored dataset manifest binding changed')
        manifest = read_json(manifest_path)
        packaged_manifest = read_json(package / 'dataset-manifest.json')
        if digest(package / 'dataset-manifest.json') != metadata['provenance']['manifest_sha256']:
            raise ValueError('Original package dataset provenance changed')
        if manifest.keys() != packaged_manifest.keys() or any(
                manifest[key] != packaged_manifest[key] for key in manifest if key != 'items'):
            raise ValueError('Restored dataset changed more than relocated input paths')
        originals = {item['id']: item for item in packaged_manifest['items']}
        if len(originals) != len(packaged_manifest['items']) or len(manifest['items']) != len(originals):
            raise ValueError('Restored dataset item scope changed')
        for item in manifest['items']:
            original = originals.get(item['id'])
            if original is None or item.keys() != original.keys() or any(
                    item[key] != original[key] for key in item if key != 'path'):
                raise ValueError('Restored dataset changed a label/split/group/source identity')
        selected = sorted((item for item in manifest['items'] if item['split'] == 'test'), key=lambda item: item['id'])
        if not selected:
            raise ValueError('Existing registered dataset has no fixed test examples')
        annotations = [dict(row) for row in db.execute('SELECT * FROM annotations ORDER BY id')]
        inputs, scoring, relocations = [], [], []
        for number, item in enumerate(selected, 1):
            asset = one(db, 'assets', item['id'])
            if any(asset[key] != item[key] for key in ('project_id', 'path', 'sha256', 'label_id', 'archived')):
                raise ValueError('Fixed test item differs from actual classification asset registration')
            path = fpath(item['path'])
            if not path.is_relative_to(workspace / 'uploads') or digest(path) != item['sha256']:
                raise ValueError('Fixed test image source/hash changed')
            target = output / 'inputs' / (str(number).zfill(4) + '-' + item['id'] + path.suffix.lower())
            frozen_bytes(target, path.read_bytes())
            inputs.append({'id': item['id'], 'image': str(target), 'image_sha256': item['sha256'],
                           'source_image': str(path), 'width': item['width'], 'height': item['height']})
            scoring.append({'id': item['id'], 'label_id': item['label_id'], 'split': item['split']})
            relocations.append({'id': item['id'], 'original_package_path': originals[item['id']]['path'],
                                'restored_source_path': str(path), 'image_sha256': item['sha256']})
    checksums = read_json(package / 'checksums.json')
    qualified = {str(package / key): sha for key, sha in checksums.items()}
    qualified.update({str(package / 'checksums.json'): digest(package / 'checksums.json'),
                      str(database): db_sha, str(manifest_path): dataset['manifest_sha256']})
    for item in inputs:
        qualified[item['source_image']] = item['image_sha256']
        qualified[item['image']] = item['image_sha256']
    if any(digest(path) != sha for path, sha in qualified.items()) or source_files(source) != source_manifest:
        raise ValueError('Read-only package/input/source changed during qualification')
    frozen_json(output / 'classification-inputs.json', {'format': FORMAT, 'truth_available_to_inference': False,
        'selection': 'All existing registered dataset test items, selected before fresh prediction; no score-based selection',
        'samples': inputs})
    frozen_json(output / 'classification-scoring.json', {'scope': 'Existing tiny synthetic engineering test split only',
        'business_validated': False, 'samples': scoring})
    metrics = json.loads(model['metrics'])
    receipt = {'format': 'specialized-published-classification-prepared-v1', 'status': 'prepared_not_inferred',
        'source_root': str(source), 'workspace_source': str(workspace), 'model_id': model_id,
        'project_id': project['id'], 'package': str(package), 'registered_model': model,
        'dataset_id': dataset['id'], 'restored_manifest_sha256': dataset['manifest_sha256'],
        'original_packaged_manifest_sha256': metadata['provenance']['manifest_sha256'],
        'relocation_only_verified': True, 'selected_input_path_mappings': relocations,
        'input_manifest': str(output / 'classification-inputs.json'),
        'input_manifest_sha256': digest(output / 'classification-inputs.json'),
        'source_files': source_manifest, 'qualified_readonly_files': qualified,
        'source_annotations_sha256': fingerprint(annotations), 'business_validated': metrics.get('business_validated'),
        'existing_model_limit': metrics.get('warning'), 'device_for_future_execution': 'cpu', 'auto_accept': False,
        'model_calls': 0, 'controller_calls': 0, 'training_calls': 0, 'production_mutations': 0,
        'platform_tool_integration_verified': False,
        'scope': 'Registered active published package and fixed engineering test inputs qualified only; no model session or Agent/native submission'}
    frozen_json(output / 'classification-prepared.json', receipt)
    return receipt


def execute(prepared_path, output):
    prepared_path, output = map(fpath, (prepared_path, output))
    prepared_sha = digest(prepared_path)
    prepared = read_json(prepared_path)
    if prepared.get('format') != 'specialized-published-classification-prepared-v1' or prepared.get('auto_accept') is not False:
        raise ValueError('Explicit previously qualified CPU classification preparation required')
    source = fpath(prepared['source_root'])
    for path, sha in prepared['qualified_readonly_files'].items():
        if digest(path) != sha: raise ValueError('Qualified read-only model/input identity changed')
    if source_files(source) != prepared['source_files'] or digest(prepared['input_manifest']) != prepared['input_manifest_sha256']:
        raise ValueError('Qualified classification source/input manifest changed')
    sys.path.insert(0, str(source / 'src'))
    from image_platform.predict import predict_images
    if not fpath(sys.modules['image_platform.predict'].__file__).is_relative_to(source / 'src'):
        raise ValueError('CPU predictor imported another classification source')
    inputs = read_json(prepared['input_manifest'])['samples']
    started = time.perf_counter()
    result = predict_images(fpath(prepared['package']), [fpath(item['image']) for item in inputs], device='cpu', auto_accept=False)
    wall = time.perf_counter() - started
    if result['providers'] != ['CPUExecutionProvider'] or result['torch_imported']:
        raise ValueError('Published model primitive did not remain CPU-only/no training framework')
    if result['model_version_id'] != prepared['model_id'] or len(result['results']) != len(inputs):
        raise ValueError('Actual CPU model output identity/scope differs')
    result['source_input_ids'] = {item['image']: item['id'] for item in inputs}
    if any(row['status'] == 'accepted' for row in result['results']):
        raise ValueError('CPU engineering example unexpectedly automatically accepted a classification')
    if (digest(prepared_path) != prepared_sha or source_files(source) != prepared['source_files']
            or any(digest(path) != sha for path, sha in prepared['qualified_readonly_files'].items())):
        raise ValueError('Read-only model/source/input changed during CPU prediction')
    receipt = {'format': 'specialized-published-classification-result-v1', 'status': 'actual_cpu_model_completed',
        'prepared': str(prepared_path), 'prepared_sha256': prepared_sha, 'python': str(fpath(sys.executable)),
        'runtime_packages': {name: runtime_version_evidence(name, module)
            for name, module in (('onnxruntime', 'onnxruntime'), ('numpy', 'numpy'), ('Pillow', 'PIL'))},
        'wall_seconds': wall, 'actual_result': result, 'auto_accept': False, 'source_annotations_unchanged': True,
        'business_validated': False, 'platform_tool_integration_verified': False,
        'scope': 'Actual existing published CPU model primitive on fixed historical synthetic test inputs; not a real Agent/native classification tool chain or business accuracy qualification',
        'model_calls': 1, 'controller_calls': 0, 'training_calls': 0, 'production_mutations': 0}
    frozen_json(output, receipt)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    for flag in ('source', 'workspace-source', 'model-id', 'output-root'):
        prep.add_argument('--' + flag, required=True)
    run = commands.add_parser('predict-cpu')
    run.add_argument('--prepared', required=True); run.add_argument('--output', required=True)
    args = parser.parse_args()
    receipt = prepare(args.source, args.workspace_source, args.model_id, args.output_root) if args.command == 'prepare' else execute(args.prepared, args.output)
    print(receipt['status'], receipt['scope'])
