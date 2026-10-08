"""CPU probe scope/route checks; no model, GPU, or real-runtime import."""
import argparse
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paddlevl_development_probe import (
    finalize_status, prepare, run, select_development, sha, source_binding, validate_frozen_route,
)


def write(path, value):
    assert path.resolve().drive.upper() == 'F:'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def development_fixture(tmp_path):
    development = tmp_path / 'development'
    image = development / 'images/development-000.png'
    write(image, 'CPU-only synthetic pixel bytes, never submitted to a model')
    manifest = tmp_path / 'inputs.json'
    payload = {'truth_available_to_inference': False,
        'samples': [{'id': 'development-000', 'image': str(image), 'image_sha256': sha(image)}]}
    write(manifest, json.dumps(payload))
    return development, manifest, payload


def test_development_selection_keeps_exact_input_identity_and_pixels(tmp_path):
    development, manifest, payload = development_fixture(tmp_path)
    assert select_development(manifest, 'development-000', development) == payload['samples'][0]


@pytest.mark.parametrize('case', ['holdout', 'duplicate', 'changed_pixels', 'embedded_truth', 'extra_reference', 'truth_allowed'])
def test_probe_selection_refuses_split_or_truth_or_identity_drift(tmp_path, case):
    development, manifest, payload = development_fixture(tmp_path)
    if case == 'holdout':
        payload['samples'][0]['image'] = str(tmp_path / 'holdout/images/development-000.png')
    elif case == 'duplicate':
        payload['samples'].append(payload['samples'][0])
    elif case == 'changed_pixels':
        payload['samples'][0]['image_sha256'] = '0' * 64
    elif case == 'embedded_truth':
        payload['truth_path'] = str(tmp_path / 'never-read-truth.json')
    elif case == 'extra_reference':
        payload['samples'][0]['reference_cells'] = ['invented']
    else:
        payload['truth_available_to_inference'] = True
    write(manifest, json.dumps(payload))
    with pytest.raises(ValueError):
        select_development(manifest, 'development-000', development)


def test_run_without_root_gpu_slot_stops_before_creating_execution_or_importing_adapter(tmp_path):
    output = tmp_path / 'uncreated-probe'
    with pytest.raises(ValueError, match='root-approved serial GPU slot'):
        run(argparse.Namespace(output=str(output), gpu_slot=None))
    assert not output.exists()


def test_existing_prepare_attempt_is_never_overwritten(tmp_path):
    output = tmp_path / 'previous-attempt'
    old = output / 'failure.json'
    write(old, '{"status":"failed"}')
    before = old.read_bytes()
    with pytest.raises(ValueError, match='unique probe output'):
        prepare(argparse.Namespace(output=str(output)))
    assert old.read_bytes() == before


def route_fixture(tmp_path, *, timeout=240, backend='native', versions=('v1', 'v1.6')):
    source, runtime = tmp_path / 'source', tmp_path / 'runtime'
    write(source / 'src/ocr_workbench/adapter.py',
        'class EngineAdapter:\n    def _load(self):\n        self.wait_for(self.ipc / "status.json", ' + str(timeout) + ')\n')
    write(source / 'src/ocr_workbench/worker.py',
        'def load_paddle():\n    return PaddleOCRVL(pipeline_version="v1.6", vl_rec_backend=' + repr(backend) +
        ', use_queues=False, use_layout_detection=True)\n')
    write(runtime / 'Lib/site-packages/paddleocr/_pipelines/paddleocr_vl.py',
        '_AVAILABLE_PIPELINE_VERSIONS = ' + repr(list(versions)))
    return source, runtime


def test_cpu_route_parse_observes_registered_native_configuration_without_importing_it(tmp_path):
    source, runtime = route_fixture(tmp_path)
    route = validate_frozen_route(source, runtime)
    assert route['startup_timeout_seconds'] == 240
    assert route['frozen_worker_pipeline_arguments']['vl_rec_backend'] == 'native'
    assert route['frozen_worker_pipeline_arguments']['use_queues'] is False


@pytest.mark.parametrize('changes', [{'timeout': 300}, {'backend': 'unreviewed'}, {'versions': ('v1', 'v1.5')}])
def test_probe_refuses_unreviewed_timeout_or_unsupported_route(tmp_path, changes):
    source, runtime = route_fixture(tmp_path, **changes)
    with pytest.raises(ValueError):
        validate_frozen_route(source, runtime)


def test_source_manifest_claim_cannot_replace_the_actual_frozen_bytes(tmp_path):
    source, freeze = tmp_path / 'source', tmp_path / 'freeze.json'
    names = ('src/ocr_workbench/adapter.py', 'src/ocr_workbench/engine_host.py',
             'src/ocr_workbench/worker.py', 'src/ocr_workbench/offline.py', 'config/screen-policy.json')
    files = {}
    for name in names:
        write(source / name, 'CPU synthetic source bytes')
        files[name] = sha(source / name)
    write(freeze, json.dumps({'sources': {'ocr': {'files': files}}}))
    assert source_binding(freeze, source)['files_sha256'] == files
    write(source / names[1], 'unfrozen replacement consumer')
    with pytest.raises(ValueError, match='Frozen OCR source differs'):
        source_binding(freeze, source)


@pytest.mark.parametrize('missing', ['owned_worker_exited', 'owned_job_closed', 'owned_job_drained',
    'reservation_released', 'observer_exited', 'source_binding_unchanged', 'helper_bytes_unchanged'])
def test_model_response_success_does_not_hide_unverified_release_or_source(missing):
    record = {'status': 'passed', 'terminal_status_receipt': {'status': 'ready'},
              'terminal_loading_receipt': {'status': 'ready'}}
    for name in ('owned_worker_exited', 'owned_job_closed', 'owned_job_drained',
                 'reservation_released', 'observer_exited', 'source_binding_unchanged', 'helper_bytes_unchanged'):
        record[name] = True
    record[missing] = False
    finalize_status(record)
    assert record['status'] == 'failed'
    assert record['error_type'] == 'UnverifiedExecutionOrCleanup'


def test_timeout_phase_and_actual_failure_are_not_replaced_by_cleanup_success():
    record = {'status': 'failed', 'error_type': 'EngineLoadTimeout', 'engine_timeout_phase': 'engine_load',
              'last_observed_stage': 'paddle_pipeline_initialization'}
    before = dict(record)
    finalize_status(record)
    assert record == before
