"""Run bundled models offline in an owned process and record local engineering evidence."""
from pathlib import Path
import argparse
from dataclasses import asdict
import hashlib
import json
import os
import shutil
import sqlite3
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
from workbench_platform.multimodal.service import MultimodalService, AUDIO_DISK_BUDGET, DISK_MARGIN, ACTIVE
from workbench_platform.multimodal.cpu_validation import record_cpu_validation, validate_inventory
from workbench_platform.multimodal import deep_backend, aigc_backend

RUNTIMES = {'face': 'runtimes/forensics/python.exe', 'audio': 'runtimes/audio/python.exe',
            'seal': 'bundles/ocr/runtimes/ppocr/python.exe', 'catnet': 'runtimes/catnet/python.exe',
            'document_forensics':'runtimes/catnet/python.exe', 'aigc_image':None}


def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--gpu-ledger', type=Path, help='Explicit shared F GPU ledger; receipts remain under --data')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--kind', choices=RUNTIMES, required=True)
    parser.add_argument('--device', choices=['cpu', 'cuda'], default='cpu')
    parser.add_argument('--backend', choices=['catnet','safire'],help='Explicit detector for deep routes; default follows current configuration')
    parser.add_argument('--aigc-engine', choices=sorted(aigc_backend.ENGINES), help='Explicit pinned AIGC research engine')
    parser.add_argument('--checkpoint-root', type=Path)
    parser.add_argument('--channel', type=int)
    parser.add_argument('--stop-after-chunks', type=int)
    parser.add_argument('--qualify', action='store_true')
    parser.add_argument('--refresh-cpu', action='store_true', help='Deprecated: sealed program receipts cannot be changed; use --cpu-receipt inside DATA')
    parser.add_argument('--cpu-receipt',type=Path,help='Write a successful CPU engineering receipt inside the selected F DATA directory')
    parser.add_argument('--trace-python-stalls',action='store_true',help='Retain a Python stack every 60 seconds in the assigned evidence output')
    parser.add_argument('--cancel-file',type=Path,help='An explicitly assigned F control file can cancel this owned diagnostic')
    args = parser.parse_args()
    config = json.loads((SOURCE/'config/multimodal.json').read_text('utf-8'))
    program, data, output, source = map(local_path, (args.program, args.data, args.output, args.input))
    if args.refresh_cpu:raise ValueError('不能修改封存程序的 CPU 回执；请使用 --cpu-receipt 指定 DATA 内的独立回执')
    cpu_receipt=local_path(args.cpu_receipt) if args.cpu_receipt else None
    cancel_file=local_path(args.cancel_file or output/'cancel.request')
    if cpu_receipt and (args.device!='cpu' or not cpu_receipt.is_relative_to(data)):
        raise ValueError('--cpu-receipt requires CPU inference and a path inside the selected F DATA')
    if args.kind=='audio':
        reserved=AUDIO_DISK_BUDGET+DISK_MARGIN
        database=data/'jobs.sqlite3'
        if database.is_file():
            with sqlite3.connect('file:'+database.as_posix()+'?mode=ro',uri=True) as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_analysis'").fetchone():
                    reserved+=sum(AUDIO_DISK_BUDGET for row in db.execute('SELECT body FROM manual_analysis')
                        if (item:=json.loads(row[0]))['kind']=='audio' and item['status'] in ACTIVE)
        if shutil.disk_usage(data if data.is_dir() else data.parent).free<reserved:
            raise ValueError(f'F 盘不足以保留音频保护预留；需至少 {reserved/1024**3:.2f} GiB，不能绕过原生服务保护')
    if output.exists() and any(output.iterdir()): raise ValueError('Use a new evidence output directory')
    output.mkdir(parents=True, exist_ok=True); data.mkdir(parents=True, exist_ok=True)
    checkpoint = local_path(args.checkpoint_root or output / 'audio-checkpoints')
    checkpoint.mkdir(parents=True, exist_ok=True)
    safire=args.kind in deep_backend.DEEP_KINDS and (args.backend or deep_backend.backend(config))=='safire'
    bridge=MultimodalService.__new__(MultimodalService)
    bridge.models,bridge.program,bridge.config=program/'models/multimodal',program,config
    bridge.qualification=Qualification(data/'gpu-qualification.json')
    bridge.safire_qualification=Qualification(data/'safire-gpu-qualification.json')
    bridge.aigc_qualification=Qualification(data/'aigc-gpu-qualification.json')
    bridge.resource_hash_cache={}
    aigc=args.kind=='aigc_image'
    pinned=aigc_backend.bind(bridge,args.aigc_engine) if aigc else deep_backend.bind(bridge,'safire' if safire else 'catnet') if args.kind in deep_backend.DEEP_KINDS else None
    runtime=local_path(pinned['runtime']) if pinned else local_path(program/RUNTIMES[args.kind])
    if safire and args.device!='cuda':raise ValueError('SAFIRE diagnostic requires CUDA; explicit CPU is not replaced')
    if not safire and not aigc:validate_inventory(program,'catnet' if args.kind=='document_forensics' else args.kind,runtime)
    if aigc and cpu_receipt:raise ValueError('AIGC CPU research cannot create a legacy product CPU qualification')
    coordinator = GpuCoordinator(local_path(args.gpu_ledger) if args.gpu_ledger else data / 'gpu.sqlite3')
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
    if pinned:
        request['model_binding']=pinned
        if aigc:request['runner']['aigc_feature_batch_size']=32
        elif safire:request['runner']['safire_points_per_batch']=64
        atomic_json(output/'request.json',request)
    worker=SOURCE/'modules/manual_multimodal'/aigc_backend.worker_entry(args.kind,pinned)
    if args.trace_python_stalls:
        bootstrap=output/'diagnostic-bootstrap.py'
        bootstrap.write_text('import faulthandler,runpy,sys\n'
            f'trace=open({str(output/"python-traceback.log")!r},"a",encoding="utf-8")\n'
            'faulthandler.enable(file=trace)\nfaulthandler.dump_traceback_later(60,repeat=True,file=trace)\n'
            f'sys.argv=[{str(worker)!r},{str(output/"request.json")!r}]\n'
            f'runpy.run_path({str(worker)!r},run_name="__main__")\n',encoding='utf-8')
        worker=bootstrap
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
        command=aigc_backend.worker_command(runtime,worker,output/'request.json') if aigc else [str(runtime),str(worker),str(output/'request.json')]
        outcome = WorkerRunner(coordinator, data / 'gpu-owners').run(
            command,
            output, env, request, request_id=request_id if args.device == 'cuda' else None,
            cancelled=lambda:cancel.is_set() or cancel_file.is_file(), progress=progress, committed=on_commit, timeout=7200)
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
        if cpu_receipt:
            record_cpu_validation(SOURCE,program,data,args.kind,runtime,output,receipt,path=cpu_receipt)
            receipt['local_cpu_receipt']=str(cpu_receipt)
        if args.qualify:
            if args.device != 'cuda' or not committed and args.kind == 'audio':
                raise ValueError('Qualification requires fresh CUDA inference, not only checkpoint reuse')
            qualification=bridge.aigc_qualification if aigc else bridge.safire_qualification if safire else bridge.qualification
            if aigc:
                from packages.contracts.multimodal import AigcImageResult
                signal=AigcImageResult.model_validate(result.get('aigc_image'))
                if (result.get('model_binding')!=pinned or result.get('source_sha256')!=digest(source)
                        or signal.preprocessing_version!=pinned['preprocessing_version']
                        or signal.calibration!=pinned.get('calibration')):
                    raise ValueError('AIGC engineering result does not match its pinned model and input')
                binding=bridge.aigc_gpu_binding(pinned)
            elif safire:
                binding=bridge.safire_gpu_binding(args.kind,pinned)
            else:
                resources=json.loads((bridge.models/'validation.json').read_text('utf-8'))['catnet' if args.kind=='document_forensics' else args.kind]['resources']
                binding=bridge.gpu_binding(args.kind,RUNTIMES[args.kind],resources)
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
