"""Real offline engine smoke checks, bound to this installation and its DATA.

These small synthetic fixtures test execution and result readback, not accuracy.
Fresh machine/GPU qualifications must be generated locally, never distributed.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import csv

PLATFORM = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLATFORM), str(PLATFORM / 'src')]
from packages.runtime import load_install, require_path

OCR = ('ppocr', 'paddlevl', 'glm', 'mineru_pro', 'hunyuan')
MANUAL = ('face', 'audio', 'seal', 'catnet', 'document_forensics', 'aigc_image')
CLASSIFICATION = tuple('classification_' + name for name in ('resnet18', 'dinov2', 'mobilenetv4', 'repvit'))
TOOLS = (*OCR, *MANUAL, *CLASSIFICATION, 'visual_review', 'classic_forensics', 'documents', 'data')


def save(path, value):
    path = require_path(path, 'Acceptance evidence', role='write')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fixtures(config, root):
    image, audio = root / 'sample.png', root / 'sample.wav'
    if not all((root / name).is_file() for name in ('sample.png', 'sample.jpg', 'sample.pdf', 'sample.wav')):
        script = '''from PIL import Image,ImageDraw,ImageFont
from pathlib import Path
import math,struct,sys,wave
root=Path(sys.argv[1]);font=Path(sys.argv[2])
image=Image.new('RGB',(768,400),'white');draw=ImageDraw.Draw(image)
f=ImageFont.truetype(str(font),32) if font.is_file() else ImageFont.load_default(size=32)
for row,text in enumerate(['CLEAR OFFLINE TEST','Invoice INV-001','Amount CNY 1,214.50','Date 2026-10-27']):
 draw.text((35,35+row*80),text,fill='black',font=f)
image.save(root/'sample.png');image.save(root/'sample.jpg',quality=95);image.save(root/'sample.pdf')
with wave.open(str(root/'sample.wav'),'wb') as w:
 w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000)
 w.writeframes(b''.join(struct.pack('<h',int(800*math.sin(2*math.pi*220*n/16000))) for n in range(48000)))
'''
        fonts = list((config.resource('program_assets') / 'bundles/ocr/fonts').glob('*.ttf'))
        font = fonts[0] if fonts else root / 'missing-font'
        command = [str(config.resource('forensics_python')), '-I', '-B', '-c', script, str(root), str(font)]
        run(command, root / 'fixture.log', config.environment())
    return image, audio


def run(command, log, environment, *, timeout=1800):
    # Installation values are managed overrides, not a complete OS environment.
    # Windows needs SystemRoot/WINDIR to initialize Winsock and native runtimes.
    child_environment = {**os.environ, **environment}
    with log.open('wb') as stream:
        result = subprocess.run(command, env=child_environment, cwd=log.parent, stdout=stream,
            stderr=subprocess.STDOUT, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError(f'Exit {result.returncode}; inspect {log}')


def ocr_worker(request_path):
    """Same gate and owned Job as manual engines; no imports before grant."""
    from modules.manual_multimodal.worker_protocol import WorkerControl
    request = json.loads(Path(request_path).read_text('utf-8'))
    output = require_path(Path(request_path).parent, 'OCR smoke output', role='write')
    control = WorkerControl(request, lambda name, value: save(output / name, value))
    if not control.enabled:
        raise ValueError('Qualification worker requires the owned-process start gate')
    control.event('ready')
    control.wait('start')
    control.check()
    verify_worker_grant(request)
    source = require_path(request['source'], 'OCR source', role='read')
    sys.path.insert(0, str(source / 'src'))
    if request['engine'] in CLASSIFICATION:
        classification_worker(request, output, control)
        control.event('completed')
        return
    if request['engine'] == 'visual_review':
        from ocr_workbench.multimodal_runtime import ReviewSession, load_config
        from ocr_workbench import platform_resources
        profile = load_config(request['bundle'], profile_id='qwen35-4b-q4')
        with borrowed_gpu_grant(platform_resources), ReviewSession(request['bundle'], profile, output/'review') as review:
            result = review.review(request['image'], {'image_sha256': digest(request['image']),
                'targets': [{'id': 'line1', 'before': 'CLEAR OFFLINE TEST', 'box': [30, 30, 700, 90]}]})
        if len(result['items']) != 1:
            raise ValueError('Visual review produced no target result')
        save(output/'result.json', {'status': 'success', 'engine': request['engine'], 'text': result['summary'], 'review': result})
    else:
        from ocr_workbench.worker import main
        sys.argv = ['ocr-smoke', '--bundle', request['bundle'], '--engine', request['engine'],
            '--image', request['image'], '--output', str(output)]
        main()
    control.check()
    result = json.loads((output / 'result.json').read_text('utf-8'))
    if result.get('status') != 'success' or result.get('engine') != request['engine']:
        raise ValueError('OCR result does not match the requested route')
    control.event('completed')


def verify_worker_grant(request):
    """The outer WorkerRunner owns the only lease for this short-lived process."""
    from workbench_platform.resources.gpu import GpuCoordinator
    from workbench_platform.resources.recovery import process_identity
    config = load_install(os.environ['CLEAR_INSTALL_CONFIG'])
    snapshot = GpuCoordinator(config.data / 'gpu.sqlite3').snapshot()
    owner = snapshot.owner
    if (owner is None or owner.requester != 'manual' or owner.pid != os.getpid()
            or owner.creation_time != process_identity(os.getpid())
            or owner.launch_id != request['runner']['launch_id']
            or owner.instance_id != request['runner']['instance_id']):
        raise ValueError('Qualification child has no matching parent GPU grant')


@contextmanager
def borrowed_gpu_grant(resources):
    """Reuse WorkerRunner's grant while retaining native child Job exit checks."""
    class Borrowed:
        def __init__(self):
            self.children = []

        def track(self, process, job):
            if not process or not job or getattr(job, 'handle', None) is None:
                raise ValueError('Review child is outside its owned Job')
            self.children.append((process, job))

        def close(self):
            if any(process.poll() is None or getattr(job, 'handle', None) is not None
                   or not getattr(job, 'drained', False) for process, job in self.children):
                raise ValueError('Review child exit is unconfirmed')
    original = resources.reserve
    resources.reserve = lambda *args, **kwargs: Borrowed()
    try:
        yield
    finally:
        resources.reserve = original


def classification_worker(request, output, control):
    """Exercise the product's dataset → train → ONNX export → prediction path."""
    config = load_install(os.environ['CLEAR_INSTALL_CONFIG'])
    sys.path.insert(0, str(config.resource('classification_source')/'src'))
    from image_platform import platform_resources, runtime, settings as native_settings
    # The parent has granted the existing GPU ledger lease to this gated Job.
    platform_resources._ACTIVE = True
    bundle = config.resource('program_assets')/'bundles/classification'
    runtime.REPO = native_settings.REPO = bundle
    sys.path.insert(0, str(bundle/'src/image_platform'))
    from platform_weights import install_weights
    from image_platform.settings import Settings
    from image_platform.store import Store
    from image_platform.demo import create_demo
    from image_platform.api import TrainingRequest
    from image_platform.training import train
    from image_platform.predict import predict_images
    from modules.manual_multimodal.worker import deny_network
    deny_network()
    settings = Settings(output/'business', lease_seconds=600)
    settings.prepare()
    install_weights(bundle, settings.data_dir)
    demo = create_demo(settings, per_class=5)
    store = Store(settings)
    params = TrainingRequest(dataset_id=demo['dataset_id'], encoder=request['engine'].removeprefix('classification_'),
        epochs=1, batch_size=4, device='cuda').model_dump()
    identifier = store.submit(demo['project_id'], 'train', params)
    job = store.claim('offline-qualification')
    if job['id'] != identifier:
        raise ValueError('Unexpected classification training identity')
    def check():
        control.check()
        store.heartbeat(job['id'], job['lease_token'])
    result = train(store, job, check, print)
    store.finish(job['id'], job['lease_token'], 'succeeded', result=result)
    model = store.one('SELECT * FROM models WHERE id=?', (result['model_id'],))
    images = [str(path) for path in (settings.data_dir/'demo-images').rglob('*.png')][:2]
    predictions = predict_images(model['package_path'], images, 'cuda', False)
    rows = predictions.get('results', [])
    if (len(images) != 2 or len(rows) != 2 or predictions.get('providers', [None])[0] != 'CUDAExecutionProvider'
            or any(row.get('status') not in {'accepted', 'needs_review'}
                   or row.get('content_sha256') != digest(row.get('path', '')) for row in rows)):
        raise ValueError('Classification prediction/readback did not complete on CUDA')
    save(output/'result.json', {'status': 'success', 'engine': request['engine'], 'text': 'synthetic classification',
        'training': result, 'predictions': predictions, 'model_package': model['package_path'],
        'dataset_id': demo['dataset_id'],
        'dataset_manifest_sha256': store.one('SELECT manifest_sha256 FROM datasets WHERE id=?', (demo['dataset_id'],))['manifest_sha256'],
        'model_sha256': digest(Path(model['package_path'])/'model.onnx')})


def check_ocr(config, engine, image, output):
    from workbench_platform.resources.gpu import GpuCoordinator
    from workbench_platform.multimodal.runner import WorkerRunner, worker_environment, PROTOCOL
    program = config.resource('program_assets')
    runtime = (config.resource('mineru_python') if engine == 'mineru_pro' else
        program/'bundles/classification/runtimes/control/python.exe' if engine in CLASSIFICATION else
        program/'bundles/ocr/runtimes/control/python.exe' if engine == 'visual_review' else
        program / 'bundles/ocr/runtimes' / engine / 'python.exe')
    coordinator = GpuCoordinator(config.data / 'gpu.sqlite3')
    if coordinator.snapshot().owner or coordinator.snapshot().pending:
        raise ValueError('GPU is occupied in this installation; finish or cancel existing tasks first')
    request_id = 'qualification-' + uuid.uuid4().hex
    request = {'engine': engine, 'image': str(image), 'bundle': str(program / 'bundles/ocr'),
        'source': str(config.resource('ocr_source')), 'options': {'device': 'cuda'},
        'runner': {'protocol': PROTOCOL, 'worker_epoch': uuid.uuid4().hex,
            'launch_id': 'local-qualification', 'instance_id': 'install-' + config.installation_id}}
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'request.json', request)
    environment = worker_environment(output, runtime, gpu=True)
    for name in ('WORKBENCH_APP_ID', 'WORKBENCH_INSTANCE_ID', 'WORKBENCH_LAUNCH_ID', 'WORKBENCH_GPU_LEDGER',
                 'WORKBENCH_NATIVE_LAUNCH_RECORD', 'WORKBENCH_NATIVE_LAUNCH_SHA256'):
        environment.pop(name, None)
    coordinator.request(request_id, 'manual')
    try:
        outcome = WorkerRunner(coordinator, config.data / 'gpu-owners').run(
            [str(runtime), '-I', '-B', str(Path(__file__).resolve()), '--ocr-worker', str(output / 'request.json')],
            output, environment, request, request_id=request_id, timeout=1800)
    finally:
        if request_id in coordinator.snapshot().pending:
            coordinator.cancel_pending(request_id, 'manual')
    if outcome.status != 'succeeded' or coordinator.snapshot().owner is not None:
        raise RuntimeError(f'OCR {engine} failed or exit unconfirmed: {asdict(outcome)}; inspect {output}')
    result = json.loads((output / 'result.json').read_text('utf-8'))
    if result.get('status') != 'success' or result.get('engine') != engine or not isinstance(result.get('text'), str):
        raise ValueError('Invalid OCR output readback')
    if engine in OCR and not result['text'].strip():
        raise ValueError('The synthetic text page produced no readable OCR text')
    inputs = {'input_sha256': digest(image)}
    if engine in CLASSIFICATION:
        package = require_path(result['model_package'], 'Classification model readback', role='read')
        if not package.is_relative_to(output) or digest(package/'model.onnx') != result['model_sha256']:
            raise ValueError('Classification exported ONNX readback differs')
        inputs = {'fixture_kind': 'synthetic-shapes', 'dataset_id': result['dataset_id'],
                  'dataset_manifest_sha256': result['dataset_manifest_sha256'],
                  'model_sha256': result['model_sha256']}
    return {'outcome': asdict(outcome), 'result_sha256': digest(output / 'result.json'),
        'engine': engine, **inputs, 'text_length': len(result['text']),
        'cleanup_confirmed': True}


def check_manual(config, engine, image, audio, output):
    command = [str(config.resource('platform_python')), '-I', '-B', str(PLATFORM / 'scripts/qualify_multimodal.py'),
        '--program', str(config.resource('program_assets')), '--data', str(config.data),
        '--output', str(output), '--input', str(audio if engine == 'audio' else image.with_suffix('.jpg')),
        '--kind', engine, '--device', 'cuda' if engine in {'audio', 'catnet', 'document_forensics', 'aigc_image'} else 'cpu']
    if engine in {'catnet', 'document_forensics'}:
        command += ['--backend', 'safire']
    if engine in {'audio', 'catnet', 'document_forensics', 'aigc_image'}:
        command += ['--qualify']
    else:
        command += ['--cpu-receipt', str(config.data / 'multimodal-cpu-validation.json')]
    run(command, output.parent / (engine + '.log'), config.environment(), timeout=3600)
    receipt = json.loads((output / 'receipt.json').read_text('utf-8'))
    if receipt.get('outcome', {}).get('status') != 'succeeded' or not receipt.get('cleanup_confirmed'):
        raise ValueError('Manual inference or owned-process exit did not succeed')
    if receipt['result_sha256'] != digest(output / 'result.json'):
        raise ValueError('Manual result readback hash differs')
    return receipt


def check_data(config, output, environment):
    """Run the real import → trim → multi-format export/readback operators."""
    uploads = output / 'uploads'
    uploads.mkdir()
    source = uploads / 'ledger.csv'
    with source.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['ID', 'Amount'])
        writer.writerow([' 001234567890 ', ' 1,214.50 '])
    state = {'tables': {'left': None, 'right': None}, 'recipes': [], 'comparisons': [], 'exports': [], 'result_id': None}
    bootstrap = "import runpy,sys;from pathlib import Path;p=Path(sys.argv.pop(1));sys.path[:0]=[str(p),str(p/'src')];runpy.run_module('modules.data_workbench.interactive_worker',run_name='__main__')"
    stages = (
        ('import', {'side': 'left', 'options': {}}),
        ('step', {'side': 'left', 'operator': 'trim_columns', 'parameters': {'columns': ['c1', 'c2']}}),
        ('export_table', {'side': 'left'}),
    )
    for action, payload in stages:
        request = {'job_id': 'offline-' + action, 'session_dir': str(output), 'action': action,
                   'state': state, 'payload': payload}
        if action == 'import':
            request['upload'] = {'id': 'offline-upload', 'relative': 'uploads/ledger.csv',
                'format': 'csv', 'name': source.name, 'sha256': digest(source)}
        request_path, response_path = output / (action + '-request.json'), output / (action + '-response.json')
        save(request_path, request)
        run([str(config.resource('data_python')), '-I', '-B', '-c', bootstrap,
             str(config.resource('platform_source')), str(request_path), str(response_path)], output/(action+'.log'), environment)
        response = json.loads(response_path.read_text('utf-8'))
        if response.get('ok') is not True:
            raise ValueError('Data workbench failed at ' + action)
        state = response['state']
    exports = state['exports'][0]['files']
    files = {}
    for item in exports:
        path = require_path(output / item['relative'], 'Data export readback', role='read')
        if not path.is_relative_to(output) or digest(path) != item['sha256']:
            raise ValueError('Data export hash or location differs')
        files[item['name']] = {'sha256': item['sha256'], 'bytes': path.stat().st_size}
    with (output / 'exports/offline-export_table/cleaned.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.reader(stream))
    if len(rows) != 2 or rows[1][1:] != ['001234567890', '1,214.50']:
        raise ValueError('Identifier or formatted amount changed in native data export')
    return {'files': files, 'identifier_and_amount_preserved': True,
            'native_actions': [action for action, payload in stages]}


def check_cpu(config, engine, image, output):
    output.mkdir(parents=True, exist_ok=False)
    environment = config.environment()
    if engine == 'classic_forensics':
        image = image.with_suffix('.jpg')
        command = [str(config.resource('forensics_python')), '-I', '-B', str(PLATFORM/'modules/image_forensics/worker.py'),
            str(image), str(output), json.dumps(['metadata', 'noise', 'frequency', 'jpeg', 'jpeg_grid', 'copy_move']), 'synthetic-offline.jpg']
        run(command, output/'run.log', environment)
        report = json.loads((output / 'report.json').read_text('utf-8'))
        expected = {'metadata', 'noise', 'frequency', 'jpeg', 'jpeg_grid', 'copy_move'}
        records = {item['tool']: item for item in report.get('evidence', [])}
        if report.get('input', {}).get('sha256') != digest(image) or expected - records.keys():
            raise ValueError('Classic forensics result/input coverage differs')
        if any(records[name]['status'] != 'completed' for name in expected):
            raise ValueError('One or more classic forensic tools did not complete')
        reports = [output / 'report.json']
        return {'reports': [{'path': str(path), 'sha256': digest(path)} for path in reports]}
    if engine == 'documents':
        command = [str(config.resource('documents_python')), '-I', '-B', str(PLATFORM/'src/workbench_platform/documents/worker.py'),
            str(image.with_suffix('.pdf')), str(output), 'pdf']
        run(command, output/'run.log', environment)
        result = json.loads((output/'raw-middle.json').read_text('utf-8'))
        if not result.get('pages'):
            raise ValueError('Document parser produced no pages')
        office_script = "from pptx import Presentation;import sys;p=Presentation();s=p.slides.add_slide(p.slide_layouts[1]);s.shapes.title.text='CLEAR OFFLINE';s.placeholders[1].text='INV-001 / CNY 1,214.50';p.save(sys.argv[1])"
        powerpoint = output / 'sample.pptx'
        run([str(config.resource('documents_python')), '-I', '-B', '-c', office_script, str(powerpoint)], output/'office-fixture.log', environment)
        from workbench_platform.comparisons.layout import prepare_pages
        pages = prepare_pages(powerpoint, {'original_name': powerpoint.name}, output/'office-render', max_pages=1)
        if len(pages) != 1 or digest(pages[0]['image_path']) != pages[0]['image_sha256']:
            raise ValueError('LibreOffice page render/readback failed')
        return {'page_count': len(result['pages']), 'result_sha256': digest(output/'raw-middle.json'),
                'office_page_sha256': pages[0]['image_sha256']}
    if engine == 'data':
        return check_data(config, output, environment)
    raise ValueError('Unknown CPU tool')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--engine', choices=(*TOOLS, 'all'), default='all')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    config = load_install(args.config)
    os.environ.update(CLEAR_INSTALL_CONFIG=str(config.path))
    os.environ.update(config.environment())
    root = require_path(args.output or config.data / 'qualification' / uuid.uuid4().hex, 'Qualification output')
    root.mkdir(parents=True, exist_ok=False)
    image, audio = fixtures(config, root)
    results = []
    for engine in TOOLS if args.engine == 'all' else (args.engine,):
        started = time.monotonic()
        try:
            output = root / engine
            details = check_ocr(config, engine, image, output) if engine in (*OCR, *CLASSIFICATION, 'visual_review') else check_manual(config, engine, image, audio, output) if engine in MANUAL else check_cpu(config, engine, image, output)
            row = {'engine': engine, 'passed': True, 'details': details}
        except Exception as exc:
            row = {'engine': engine, 'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
        row['seconds'] = round(time.monotonic() - started, 3)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        save(root / 'results.json', {'format': 'clear-portable-engine-smoke-v1', 'installation_id': config.installation_id,
            'package_manifest_sha256': digest(config.program / 'manifests/package.json'), 'results': results,
            'complete': len(results) == len(TOOLS) and all(row['passed'] for row in results),
            'status': 'passed' if len(results) == len(TOOLS) and all(row['passed'] for row in results) else 'partial' if all(row['passed'] for row in results) else 'blocked',
            'scope': 'Local offline execution/readback; accuracy and independent-machine qualification excluded'})
    return 0 if all(row['passed'] for row in results) else 2


if __name__ == '__main__':
    if sys.argv[1:2] == ['--ocr-worker']:
        ocr_worker(sys.argv[2])
    else:
        raise SystemExit(main())
