"""Portable first-week command line acceptance interface."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import uuid

ENGINES = {'ppocr': 'ppocr', 'paddlevl': 'paddlevl', 'glm': 'glm', 'hunyuan': 'hunyuan'}


def bundle_root():
    return Path(__file__).resolve().parents[2]


def run_engine(root, engine, image, output, timeout=900):
    from ocr_workbench.processes import ProcessJob
    if not image.is_file():
        raise FileNotFoundError(image)
    runtime = root / 'runtimes' / ENGINES[engine] / 'python.exe'
    if not runtime.is_file():
        raise FileNotFoundError(f'Missing runtime: {runtime}')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory must be empty to prevent stale acceptance evidence')
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    # Only bundled DLLs and Windows system DLLs may resolve through PATH.
    env['PATH'] = str(runtime.parent) + os.pathsep + str(Path(os.environ['SystemRoot']) / 'System32')
    for name in list(env):
        if name.upper() in {'PYTHONPATH', 'PYTHONHOME', 'CUDA_PATH', 'CUDA_HOME'} or name.startswith('CUDA_PATH_V'):
            del env[name]
    cmd = [str(runtime), '-X', 'utf8', '-I', '-m', 'ocr_workbench.worker', '--bundle', str(root),
           '--engine', engine, '--image', str(image.resolve()), '--output', str(output.resolve())]
    with ProcessJob() as job, (output / 'worker.log').open('w', encoding='utf-8') as log:
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(proc)
        try:
            code = proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            job.close()
            proc.wait(timeout=15)
            raise
    if code != 0:
        raise RuntimeError(f'{engine} failed ({code}); see {output / "worker.log"}')
    result_path = output / 'result.json'
    if not result_path.is_file():
        raise RuntimeError('Worker exited without a result')
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if result['status'] != 'success':
        raise RuntimeError('Worker did not report success')
    return result


def verify(root):
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    errors = []
    for item in manifest['files']:
        path = (root / item['path']).resolve()
        if not path.is_relative_to(root.resolve()):
            errors.append(f'Unsafe manifest path: {item["path"]}')
        elif not path.is_file():
            errors.append(f'Missing: {item["path"]}')
        else:
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != item['sha256']:
                errors.append(f'Hash mismatch: {item["path"]}')
    return errors


def main():
    parser = argparse.ArgumentParser(description='Offline OCR week-one compatibility kit')
    parser.add_argument('--bundle', type=Path, default=bundle_root())
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    sub.add_parser('verify')
    run = sub.add_parser('recognize')
    run.add_argument('--engine', choices=[*ENGINES, 'all'], required=True)
    run.add_argument('--image', type=Path, required=True)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    root = args.bundle.resolve()
    if args.command == 'doctor':
        missing = [str(root/'runtimes'/name/'python.exe') for name in ENGINES.values()
                   if not (root/'runtimes'/name/'python.exe').is_file()]
        registry = json.loads((root/'config/engines.json').read_text(encoding='utf-8'))
        for info in registry.values():
            for name in info['models']:
                if not (root/'models'/name/'source-manifest.json').is_file():
                    missing.append(str(root/'models'/name/'source-manifest.json'))
        gpu = subprocess.run(['nvidia-smi', '--query-gpu=name,driver_version,memory.total,memory.free',
                              '--format=csv,noheader'], capture_output=True, text=True, timeout=20)
        print(json.dumps({'platform': platform.platform(), 'python': sys.version,
            'bundle': str(root), 'gpu': gpu.stdout.strip(), 'gpu_exit': gpu.returncode,
            'disk_free_bytes': shutil.disk_usage(root).free, 'missing': missing,
            'runtimes': {engine: (root/'runtimes'/name/'python.exe').is_file() for engine,name in ENGINES.items()}},
            ensure_ascii=False, indent=2))
        return 0 if gpu.returncode == 0 and not missing else 1
    if args.command == 'verify':
        errors = verify(root)
        print(json.dumps({'status': 'failed' if errors else 'success', 'errors': errors}, ensure_ascii=False, indent=2))
        return bool(errors)
    import msvcrt
    # A per-user lock also serializes separate relocated copies of the kit.
    lock_path = Path(os.environ.get('LOCALAPPDATA', str(root))) / 'OfflineOCR' / 'gpu.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('Another OfflineOCR process owns the GPU; wait for it to exit')
        failures = []
        for engine in ENGINES if args.engine == 'all' else [args.engine]:
            dest = args.output / engine if args.engine == 'all' else args.output
            try:
                result = run_engine(root, engine, args.image, dest, args.timeout)
                print(json.dumps({'engine': engine, 'status': 'success', 'seconds': result['elapsed_seconds'],
                                  'blocks': len(result['blocks']), 'tables': len(result['tables'])}), flush=True)
            except Exception as error:
                failures.append(engine)
                print(json.dumps({'engine': engine, 'status': 'failed', 'message': str(error)}), flush=True)
        return bool(failures)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(json.dumps({'status': 'cancelled'}))
        raise SystemExit(130)
    except Exception as error:
        print(json.dumps({'status': 'failed', 'message': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
