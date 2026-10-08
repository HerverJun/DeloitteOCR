"""One prepared development-only PaddleOCR-VL adapter probe.

Prepare hashes existing F-drive sources/runtime/models without importing Paddle.
Run requires an explicit root-owned GPU slot; it retains every startup/inference
receipt and unloads only the worker it created. It does not qualify Agent or OCR
accuracy and never reads truth/holdout data or changes the installed bundle.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback


def fpath(value):
    requested = Path(value).absolute()
    resolved = requested.resolve()
    if requested.drive.upper() != 'F:' or resolved.drive.upper() != 'F:':
        raise ValueError('All probe paths must resolve to the local F drive')
    if requested.is_symlink() or requested.is_junction():
        raise ValueError('Probe paths must not be links')
    return resolved


def sha(path):
    with fpath(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read(path):
    return json.loads(fpath(path).read_text(encoding='utf-8'))


def write_new(path, value):
    path = fpath(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def inventory(root):
    root = fpath(root)
    records = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Probe input inventory must not contain links')
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
            actual = fpath(path)
            if not actual.is_relative_to(root):
                raise ValueError('Probe input escaped its declared root')
            records[path.relative_to(root).as_posix()] = {'bytes': path.stat().st_size, 'sha256': sha(path)}
    if not records:
        raise ValueError('Probe input inventory is empty')
    return records


def select_development(inputs, sample_id, development_root):
    inputs, development = fpath(inputs), fpath(development_root)
    if development.name != 'development':
        raise ValueError('Probe must bind the development split explicitly')
    document = read(inputs)
    if document.get('truth_available_to_inference') is not False:
        raise ValueError('Development input manifest must exclude inference truth')
    samples = [sample for sample in document.get('samples', []) if sample.get('id') == sample_id]
    if len(samples) != 1 or not sample_id.startswith('development-'):
        raise ValueError('A unique declared development sample is required')
    sample = samples[0]
    if set(sample) != {'id', 'image', 'image_sha256'} or any(name in document for name in
            ('truth', 'truth_path', 'truth_json', 'reference_cells')):
        raise ValueError('Probe input manifest must contain pixels and identities only')
    image = fpath(sample['image'])
    if not image.is_relative_to(development / 'images'):
        raise ValueError('Probe input is outside development images')
    if sha(image) != sample['image_sha256']:
        raise ValueError('Development input pixels differ from their manifest')
    return {'id': sample_id, 'image': str(image), 'image_sha256': sample['image_sha256']}


def source_binding(freeze, source):
    freeze, source = fpath(freeze), fpath(source)
    record = read(freeze)['sources']['ocr']
    for relative, expected in record['files'].items():
        path = fpath(source / relative)
        if not path.is_relative_to(source) or sha(path) != expected:
            raise ValueError('Frozen OCR source differs: ' + relative)
    required = ('src/ocr_workbench/adapter.py', 'src/ocr_workbench/engine_host.py',
                'src/ocr_workbench/worker.py', 'src/ocr_workbench/offline.py', 'config/screen-policy.json')
    if not all(name in record['files'] for name in required):
        raise ValueError('Frozen source does not cover the actual adapter/worker/policy')
    return {'root': str(source), 'freeze': str(freeze), 'freeze_sha256': sha(freeze),
            'files_sha256': record['files'], 'inventory_sha256': fingerprint(record['files'])}


def validate_frozen_route(source, runtime):
    source, runtime = fpath(source), fpath(runtime)
    adapter_ast = ast.parse((source / 'src/ocr_workbench/adapter.py').read_text(encoding='utf-8'))
    loads = [node for node in ast.walk(adapter_ast) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'wait_for'
        and len(node.args) == 2 and any(isinstance(part, ast.Constant) and part.value == 'status.json'
                                      for part in ast.walk(node.args[0]))]
    if len(loads) != 1 or not isinstance(loads[0].args[1], ast.Constant) or loads[0].args[1].value != 240:
        raise ValueError('Probe must retain the observed existing 240-second startup budget')
    pipeline = runtime / 'Lib/site-packages/paddleocr/_pipelines/paddleocr_vl.py'
    pipeline_ast = ast.parse(pipeline.read_text(encoding='utf-8'))
    versions = [ast.literal_eval(node.value) for node in pipeline_ast.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == '_AVAILABLE_PIPELINE_VERSIONS'
                        for target in node.targets)]
    if len(versions) != 1 or 'v1.6' not in versions[0]:
        raise ValueError('Installed PaddleOCR pipeline does not declare v1.6 support')
    worker_ast = ast.parse((source / 'src/ocr_workbench/worker.py').read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(worker_ast) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'PaddleOCRVL']
    if len(calls) != 1:
        raise ValueError('Frozen worker must have one known PaddleOCR-VL construction route')
    settings = {keyword.arg: ast.literal_eval(keyword.value) for keyword in calls[0].keywords
                if keyword.arg in ('pipeline_version', 'vl_rec_backend', 'use_queues', 'use_layout_detection')}
    if settings != {'pipeline_version': 'v1.6', 'vl_rec_backend': 'native', 'use_queues': False,
                    'use_layout_detection': True}:
        raise ValueError('Frozen registered native PaddleOCR-VL route differs')
    return {'startup_timeout_seconds': 240, 'installed_pipeline_versions': versions[0],
            'installed_pipeline_module': {'path': str(pipeline), 'sha256': sha(pipeline)},
            'frozen_worker_pipeline_arguments': settings}


def installed_distributions(runtime):
    records = {}
    for metadata in sorted((fpath(runtime) / 'Lib/site-packages').glob('*.dist-info/METADATA')):
        message = Parser().parsestr(metadata.read_text(encoding='utf-8'))
        records[message['Name']] = {'version': message['Version'], 'metadata_sha256': sha(metadata)}
    for name in ('paddleocr', 'paddlex', 'paddlepaddle-gpu'):
        if name not in records:
            raise ValueError('Installed required distribution missing: ' + name)
    return records


def prepare(args):
    output = fpath(args.output)
    if output.exists():
        raise ValueError('Use a unique probe output; existing attempts are immutable')
    sample = select_development(args.inputs, args.sample_id, args.development_root)
    sources = source_binding(args.source_freeze, args.source)
    bundle = fpath(args.bundle)
    engine = read(bundle / 'config/engines.json')['paddlevl']
    if engine['models'] != ['PaddleOCR-VL-1.6', 'PP-DocLayoutV3']:
        raise ValueError('Registered PaddleOCR-VL model route differs')
    runtime = bundle / 'runtimes/paddlevl'
    route = validate_frozen_route(sources['root'], runtime)
    distributions = installed_distributions(runtime)
    # Read all runtime bytes, excluding pre-existing Python bytecode caches.
    # The bundle remains read-only. No runtime process or framework is imported.
    runtime_files = inventory(runtime)
    model_files, manifest_checks = {}, {}
    for model in engine['models']:
        root = bundle / 'models' / model
        files = inventory(root)
        model_files.update({model + '/' + relative: record for relative, record in files.items()})
        manifest = read(root / 'source-manifest.json')
        for declared in manifest['files']:
            # Historical drive names are metadata only; read F basenames.
            relative = Path(declared['file']).name
            actual = files[relative]
            if actual['bytes'] != declared['bytes'] or actual['sha256'] != declared['sha256']:
                raise ValueError('Registered model manifest mismatch: ' + model + '/' + relative)
        manifest_checks[model] = {'revision': manifest['revision'], 'declared_files': len(manifest['files']),
                                  'manifest_sha256': files['source-manifest.json']['sha256']}
    policy = read(Path(sources['root']) / 'config/screen-policy.json')
    output.mkdir(parents=True)
    write_new(output / 'runtime-inventory.json', {'root': str(runtime), 'files': runtime_files,
        'inventory_sha256': fingerprint(runtime_files), 'distributions': distributions,
        'excluded': ['__pycache__', '*.pyc']})
    write_new(output / 'model-inventory.json', {'root': str(bundle / 'models'), 'files': model_files,
        'inventory_sha256': fingerprint(model_files), 'source_manifest_checks': manifest_checks})
    write_new(output / 'source-binding.json', sources)
    prepared = {'format': 'paddlevl-development-probe-prepared-v1', 'status': 'prepared',
        'prepared_at': datetime.now(timezone.utc).isoformat(), 'sample': sample,
        'inputs': {'path': str(fpath(args.inputs)), 'sha256': sha(args.inputs)},
        'development_root': str(fpath(args.development_root)), 'bundle': str(bundle),
        'registered_engine': engine, 'engine_config_sha256': sha(bundle / 'config/engines.json'),
        'runtime_route_validation': route,
        'source_binding': {'path': str(output / 'source-binding.json'), 'sha256': sha(output / 'source-binding.json')},
        'runtime_inventory': {'path': str(output / 'runtime-inventory.json'), 'sha256': sha(output / 'runtime-inventory.json')},
        'model_inventory': {'path': str(output / 'model-inventory.json'), 'sha256': sha(output / 'model-inventory.json')},
        'helper': {'path': str(Path(__file__).resolve()), 'sha256': sha(__file__)},
        'startup_timeout_seconds': 240, 'recognition_timeout_seconds': policy['request_timeout_seconds'],
        'policy_timeout_basis': 'Exact frozen current screen-policy request timeout; no timeout increase',
        'scope_inputs': 1, 'truth_read': False, 'holdout_read': False, 'gpu_started': False,
        'model_calls': 0, 'controller_calls': 0, 'actual_agent': False,
        'observable_stages': ['worker_start', 'offline_import', 'offline_configuration', 'dependency_import',
            'model_path_preparation', 'paddle_import', 'paddle_pipeline_initialization', 'readiness_publish', 'ready'],
        'limits': ['CPU preparation is not load or inference success',
            'Pipeline stage includes PaddleOCR imports and model construction; internals are not separately observed',
            'One fresh worker/cache does not establish repeated loading reliability or machine-cold performance',
            'Raw full development image is used; no truth, raw replay, crop substitution, or automatic adoption']}
    write_new(output / 'prepared.json', prepared)
    print(json.dumps({'status': 'prepared', 'sample': sample['id'], 'runtime_files': len(runtime_files),
        'model_files': len(model_files), 'startup_timeout_seconds': 240,
        'recognition_timeout_seconds': prepared['recognition_timeout_seconds'], 'gpu_started': False,
        'prepared': str(output / 'prepared.json')}), flush=True)


def verify_prepared(prepared):
    for name in ('source_binding', 'runtime_inventory', 'model_inventory', 'helper', 'inputs'):
        reference = prepared[name]
        if sha(reference['path']) != reference['sha256']:
            raise ValueError('Prepared binding changed: ' + name)
    sample = select_development(prepared['inputs']['path'], prepared['sample']['id'], prepared['development_root'])
    if sample != prepared['sample']:
        raise ValueError('Prepared image selection changed')
    sources = read(prepared['source_binding']['path'])
    if source_binding(sources['freeze'], sources['root']) != sources:
        raise ValueError('Prepared source binding changed')
    for name in ('runtime_inventory', 'model_inventory'):
        binding = read(prepared[name]['path'])
        for relative, record in binding['files'].items():
            path = fpath(Path(binding['root']) / relative)
            if not path.is_relative_to(fpath(binding['root'])) or path.stat().st_size != record['bytes'] or sha(path) != record['sha256']:
                raise ValueError('Prepared model/runtime bytes changed: ' + relative)
    if sha(Path(prepared['bundle']) / 'config/engines.json') != prepared['engine_config_sha256']:
        raise ValueError('Registered engine configuration changed')
    if validate_frozen_route(sources['root'], Path(prepared['bundle']) / 'runtimes/paddlevl') != prepared['runtime_route_validation']:
        raise ValueError('Prepared runtime construction route changed')
    return sources


def finalize_status(record):
    """A successful response does not prove readiness or release of ownership."""
    verified = all(record.get(key) is True for key in ('owned_worker_exited', 'owned_job_closed',
        'owned_job_drained', 'reservation_released', 'observer_exited', 'source_binding_unchanged', 'helper_bytes_unchanged'))
    if record['status'] == 'passed' and (not verified or
            record.get('terminal_status_receipt', {}).get('status') != 'ready' or
            record.get('terminal_loading_receipt', {}).get('status') != 'ready'):
        record.update(status='failed', error_type='UnverifiedExecutionOrCleanup',
                      error='Model success cannot replace verified readiness, frozen source bytes and owned cleanup')


def run(args):
    if not args.gpu_slot:
        raise ValueError('Actual probe requires the root-approved serial GPU slot; prepare never starts it')
    output = fpath(args.output)
    prepared = read(output / 'prepared.json')
    sources = verify_prepared(prepared)
    execution = output / 'execution'
    if execution.exists():
        raise ValueError('Probe executes once; preserve this attempt and use a new prepared root to retry')
    execution.mkdir()
    for name in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'XDG_CACHE_HOME', 'MPLCONFIGDIR',
                 'HF_HOME', 'TORCH_HOME', 'PADDLE_HOME', 'PADDLE_PDX_CACHE_HOME', 'PADDLE_EXTENSION_DIR'):
        directory = execution / 'environment' / name.lower()
        directory.mkdir(parents=True)
        os.environ[name] = str(directory)
    # Standalone existing EngineAdapter compatibility mode. Root serializes all
    # GPU experiments; this is not a platform/Agent coordinator qualification.
    if any(os.environ.get(key) for key in ('WORKBENCH_APP_ID', 'WORKBENCH_INSTANCE_ID',
                                         'WORKBENCH_LAUNCH_ID', 'WORKBENCH_GPU_LEDGER')):
        raise ValueError('Independent primitive probe must not inherit a live platform GPU scope')
    worker_source = fpath(Path(sources['root']) / 'src')
    sys.path.insert(0, str(worker_source))
    from ocr_workbench import adapter as product_adapter
    if fpath(product_adapter.__file__) != worker_source / 'ocr_workbench/adapter.py':
        raise ValueError('Actual adapter import escaped its frozen source')
    adapter = product_adapter.EngineAdapter(prepared['bundle'], 'paddlevl', execution / 'sessions', worker_source=worker_source)
    adapter.recognition_timeout = prepared['recognition_timeout_seconds']
    stop = threading.Event()
    phase = {'name': 'load'}
    def observe():
        previous = None
        with (execution / 'loading-observations.jsonl').open('x', encoding='utf-8') as stream:
            while not stop.is_set():
                path = adapter.ipc / 'loading.json'
                try:
                    payload = path.read_bytes()
                    if payload != previous:
                        progress = json.loads(payload)
                        stream.write(json.dumps({'observed_unix': time.time(), 'phase': phase['name'],
                            'original_receipt_sha256': hashlib.sha256(payload).hexdigest(), 'receipt': progress}) + '\n')
                        stream.flush()
                        previous = payload
                except (OSError, ValueError):
                    pass
                if (execution / 'control-stop.json').exists():
                    adapter.cancel()
                stop.wait(0.25)
    monitor = threading.Thread(target=observe, name='paddlevl-probe-observer', daemon=True)
    started = time.perf_counter()
    record = {'format': 'paddlevl-development-probe-result-v1', 'status': 'running',
        'sample': prepared['sample'], 'prepared_sha256': sha(output / 'prepared.json'),
        'helper_sha256_at_start': sha(__file__),
        'control_runtime': {'path': str(fpath(sys.executable)), 'sha256': sha(sys.executable)},
        'gpu_slot': args.gpu_slot, 'actual_command_argv': list(sys.orig_argv), 'cwd': str(fpath(Path.cwd())),
        'scope_inputs': 1, 'actual_adapter': True, 'actual_agent': False, 'truth_read': False,
        'holdout_read': False, 'controller_calls': 0, 'load_calls': 0, 'inference_calls': 0,
        'startup_timeout_seconds': 240, 'recognition_timeout_seconds': adapter.recognition_timeout,
        'fresh_worker_and_session_cache': True, 'os_disk_cache_flushed': False,
        'worker_session': str(adapter.ipc), 'source_inventory_sha256': sources['inventory_sha256']}
    owned_job = None
    monitor.start()
    try:
        record['load_calls'] = 1
        before = time.perf_counter()
        adapter.load()
        record.update(load_wall_seconds=time.perf_counter() - before, ready=True,
                      worker_pid=adapter.process.pid, worker_command_argv=adapter.process.args)
        owned_job = adapter.job
        phase['name'] = 'infer'
        before = time.perf_counter()
        record['inference_calls'] = 1
        result = adapter.recognize(Path(prepared['sample']['image']), execution / 'raw')
        record['inference_wall_seconds'] = time.perf_counter() - before
        write_new(execution / 'adapter-result.json', result)
        record.update(status='passed', model_result_status=result.get('status'),
                      result={'path': str(execution / 'adapter-result.json'), 'sha256': sha(execution / 'adapter-result.json')})
    except BaseException as error:
        record.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
            failure_phase=phase['name'], error_type=type(error).__name__, error=str(error),
            traceback=traceback.format_exc(), engine_timeout_phase=getattr(error, 'phase', None),
            last_observed_stage=getattr(error, 'last_stage', None),
            interrupted_phase_wall_seconds=time.perf_counter() - before,
            stop_request_exists=(execution / 'control-stop.json').exists())
    finally:
        phase['name'] = 'owned_cleanup'
        if adapter.job is not None:
            owned_job = adapter.job
        process = adapter.process
        if process is not None:
            record.setdefault('worker_pid', process.pid)
            record.setdefault('worker_command_argv', process.args)
        try:
            adapter.unload()
            record.update(owned_worker_exited=process is None or process.poll() is not None,
                worker_exit_code=process.poll() if process is not None else None,
                owned_job_closed=owned_job is None or owned_job.handle is None,
                owned_job_drained=owned_job is None or owned_job.drained,
                reservation_released=adapter.reservation is None)
        except BaseException as error:
            record.update(status='failed', cleanup_error_type=type(error).__name__, cleanup_error=str(error))
        stop.set()
        monitor.join(timeout=5)
        record['observer_exited'] = not monitor.is_alive()
        try:
            record['source_binding_unchanged'] = source_binding(sources['freeze'], sources['root']) == sources
        except (ValueError, KeyError, OSError) as error:
            record.update(source_binding_unchanged=False, source_verification_error=str(error))
        record['helper_sha256_at_exit'] = sha(__file__)
        record['helper_bytes_unchanged'] = (record['helper_sha256_at_start'] ==
            record['helper_sha256_at_exit'] == prepared['helper']['sha256'])
        for name in ('status', 'loading'):
            path = adapter.ipc / (name + '.json')
            if path.exists():
                record['terminal_' + name + '_receipt'] = read(path)
        finalize_status(record)
        record['wall_seconds'] = time.perf_counter() - started
        record['exit_origin'] = ('root_control_stop_request_then_owned_unload' if record.get('stop_request_exists')
            else 'external_keyboard_interrupt_then_owned_unload' if record.get('status') == 'interrupted'
            else 'probe_owned_unload_after_' + record['status'])
        record['retained_files'] = inventory(execution)
        # Actual raw/model receipts survive failures and timeout cleanup.
        write_new(execution / 'result.json', record)
    print(json.dumps({key: record.get(key) for key in ('status', 'failure_phase', 'error_type',
        'last_observed_stage', 'load_wall_seconds', 'inference_wall_seconds', 'owned_worker_exited', 'wall_seconds')}), flush=True)
    return 0 if record['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'run'))
    parser.add_argument('--output', required=True)
    parser.add_argument('--gpu-slot')
    for name in ('source', 'source-freeze', 'bundle', 'inputs', 'sample-id', 'development-root'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    if args.mode == 'prepare':
        if any(getattr(args, name.replace('-', '_')) is None for name in
               ('source', 'source-freeze', 'bundle', 'inputs', 'sample-id', 'development-root')):
            parser.error('Prepare requires source/freeze, bundle, development inputs/root and sample ID')
        prepare(args)
        return 0
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
