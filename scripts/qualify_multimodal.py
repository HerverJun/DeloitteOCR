"""Run bundled models offline in an owned process and record local engineering evidence."""
from pathlib import Path
import argparse
from dataclasses import asdict
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'src'))
sys.path.insert(0, str(SOURCE))
from workbench_platform.resources.gpu import GpuCoordinator
from workbench_platform.resources.recovery import local_path, atomic_json
from workbench_platform.multimodal.runner import WorkerRunner, worker_environment, PROTOCOL
from workbench_platform.multimodal.qualification import Qualification
from workbench_platform.multimodal.service import MultimodalService

RUNTIMES = {'face': 'runtimes/forensics/python.exe', 'audio': 'runtimes/audio/python.exe',
            'seal': 'bundles/ocr/runtimes/ppocr/python.exe', 'catnet': 'runtimes/catnet/python.exe'}


def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--kind', choices=RUNTIMES, required=True)
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--checkpoint-root', type=Path)
    parser.add_argument('--channel', type=int)
    parser.add_argument('--stop-after-chunks', type=int)
    parser.add_argument('--qualify', action='store_true')
    parser.add_argument('--refresh-cpu', action='store_true', help='Refresh bundled CPU engineering receipt after successful CPU inference')
    args = parser.parse_args()
    config = json.loads((SOURCE/'config/multimodal.json').read_text('utf-8'))
    program, data, output, source = map(local_path, (args.program, args.data, args.output, args.input))
    if output.exists() and any(output.iterdir()): raise ValueError('Use a new evidence output directory')
    output.mkdir(parents=True, exist_ok=True); data.mkdir(parents=True, exist_ok=True)
    checkpoint = local_path(args.checkpoint_root or output / 'audio-checkpoints')
    checkpoint.mkdir(parents=True, exist_ok=True)
    runtime = local_path(program / RUNTIMES[args.kind])
    coordinator = GpuCoordinator(data / 'gpu.sqlite3')
    request_id = 'qualify' + uuid.uuid4().hex
    if args.device == 'cuda':
        if coordinator.snapshot().owner or coordinator.snapshot().pending:
            raise ValueError('GPU ledger is occupied; finish earlier tasks before qualification')
        coordinator.request(request_id, 'manual')
    request = {'kind': args.kind, 'models': str(program / 'models/multimodal'), 'program': str(program),
        'inputs': [{'asset_id': 'aEngineering', 'asset_revision': 1, 'blob_hash': digest(source), 'path': str(source)}],
        'references': [], 'options': {'device': args.device, 'action': 'detect' if args.kind == 'face' else 'analyze',
            'face_indices': [0, 0], 'top_k': 5, 'threshold': .363, 'expected_subject': '', 'channel': args.channel},
        'runner': {'protocol': PROTOCOL, 'worker_epoch': uuid.uuid4().hex, 'launch_id': 'qualification',
            'instance_id': 'qualification', 'job_id': request_id, 'generation': 1,
            'checkpoint_root': str(checkpoint), 'audio_batch_size_s': 30,
            'catnet_tile_size': config.get('catnet',{}).get('tile_size',1024),
            'catnet_halo': config.get('catnet',{}).get('halo',0)}}
    atomic_json(output / 'request.json', request)
    env = worker_environment(output, runtime, gpu=args.device == 'cuda')
    if args.kind == 'seal': env.update(OMP_NUM_THREADS='1', MKL_NUM_THREADS='8', OPENBLAS_NUM_THREADS='1')
    started = time.monotonic()
    peak, stopped, cancel, committed = [0.0], threading.Event(), threading.Event(), []
    executable = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/nvidia-smi.exe'

    def sample():
        while not stopped.is_set():
            try:
                response = subprocess.run([str(executable), '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if response.returncode == 0:
                    peak[0] = max(peak[0], float(response.stdout.splitlines()[0].strip()))
            except (OSError, ValueError, subprocess.TimeoutExpired): pass
            stopped.wait(1)

    def progress(value):
        atomic_json(output / 'observed-progress.json', {'elapsed_seconds': round(time.monotonic()-started, 2), **value})

    def on_commit(value):
        committed.append(value)
        atomic_json(output / 'commits.json', committed)
        print(f"Committed {value['chunk_index']+1}: {value['processed_ms']/1000:.1f}s", flush=True)
        if args.stop_after_chunks and len(committed) >= args.stop_after_chunks: cancel.set()

    monitor = threading.Thread(target=sample, daemon=True) if args.device == 'cuda' else None
    if monitor: monitor.start()
    try:
        outcome = WorkerRunner(coordinator, data / 'gpu-owners').run(
            [str(runtime), str(SOURCE / 'modules/manual_multimodal/worker.py'), str(output / 'request.json')],
            output, env, request, request_id=request_id if args.device == 'cuda' else None,
            cancelled=cancel.is_set, progress=progress, committed=on_commit, timeout=7200)
    finally:
        stopped.set()
        if monitor: monitor.join(timeout=15)
    receipt = {'kind': args.kind, 'requested_device': args.device, 'outcome': asdict(outcome),
        'seconds': round(time.monotonic()-started, 3), 'sampled_peak_device_mib': peak[0],
        'committed_chunks': len(committed), 'output': str(output),
        'cleanup_confirmed': outcome.status != 'needs_verification' and (args.device != 'cuda' or coordinator.snapshot().owner is None), 'cold_cache': True,
        'scope': 'Offline engineering sample; no business or independent site qualification'}
    if outcome.status == 'succeeded':
        result_path = output / 'result.json'
        result = json.loads(result_path.read_text('utf-8'))
        receipt.update(result_sha256=digest(result_path), result_outcome=result['outcome'],
                       actual_device=result['device'], performance=result.get('performance', {}))
        if args.kind == 'audio':
            duration = result['audio']['duration_ms'] / 1000
            receipt['duration_seconds'] = duration
            receipt['rtf_including_load_decode_export'] = receipt['seconds'] / duration
            receipt['segments'] = len(result['segments'])
        atomic_json(output / 'receipt.json', receipt)
        if args.refresh_cpu:
            if args.device != 'cpu': raise ValueError('--refresh-cpu requires CPU inference')
            receipt_path=program/'models/multimodal/validation.json'
            records=json.loads(receipt_path.read_text('utf-8'))
            resources=records[args.kind]['resources']
            for name,identity in resources.items():
                path=program/'models/multimodal'/name
                if path.stat().st_size!=identity['bytes'] or digest(path)!=identity['sha256']:
                    raise ValueError('Model resource changed since bundled inventory: '+name)
            records[args.kind]={'cpu':True,'gpu':False,'resources':resources,
                'adapter_sha256':digest(SOURCE/'modules/manual_multimodal/worker.py'),
                'source_hashes':{path.relative_to(SOURCE).as_posix():digest(path) for path in sorted((SOURCE/'modules/manual_multimodal').glob('*.py'))},
                'runtime_sha256':digest(runtime),'record':{**receipt,'exit_code':0},
                'scope':'Cold-cache CPU engineering inference; business and independent-site qualification remain pending'}
            atomic_json(receipt_path,records)
            receipt['cpu_receipt_refreshed']=True
        if args.qualify:
            if args.device != 'cuda' or not committed and args.kind == 'audio':
                raise ValueError('Qualification requires fresh CUDA inference, not only checkpoint reuse')
            qualification = Qualification(data / 'gpu-qualification.json')
            bridge = MultimodalService.__new__(MultimodalService)
            bridge.models, bridge.program, bridge.qualification = program / 'models/multimodal', program, qualification
            bridge.config=json.loads((SOURCE/'config/multimodal.json').read_text('utf-8'))
            resources = json.loads((bridge.models / 'validation.json').read_text('utf-8'))[args.kind]['resources']
            binding = bridge.gpu_binding(args.kind, RUNTIMES[args.kind], resources)
            evidence = {'succeeded': True, 'actual_device': result['device'], 'cold_cache': True,
                'network_blocked': result.get('network_policy') == 'all_outbound_connections_denied',
                'cleanup_confirmed': receipt['cleanup_confirmed'], 'memory_method': 'device_total',
                'peak_memory_mib': peak[0], 'result_sha256': receipt['result_sha256'],
                'execution': result.get('execution', {}),
                'receipt': str(output / 'receipt.json')}
            qualification.record(args.kind, binding, evidence)
            receipt['local_gpu_qualified'] = True
    atomic_json(output / 'receipt.json', receipt)
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    return 0 if outcome.status == 'succeeded' else 2


if __name__ == '__main__':
    raise SystemExit(main())
