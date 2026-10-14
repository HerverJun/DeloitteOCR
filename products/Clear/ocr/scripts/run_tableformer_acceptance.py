"""Run the sealed fresh test sequentially; score only after every inference stage.

The historical inference adapters and scorer are reused without changing their
thresholds. All subprocesses finish before the next GPU stage starts.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geometry_eval_common import ROOT
from develop_tableformer_v3 import check_test


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--rapid-python', type=Path, default=Path('D:/anaconda3/python.exe'))
    args = parser.parse_args()
    root, bundle = args.root.resolve(), args.bundle.resolve()
    manifest = root / 'dataset/inputs/test.json'
    seal = root / 'selection-seal.json'
    check_test(json.loads(manifest.read_text('utf-8')), seal, manifest)
    service = bundle / 'runtimes/service/python.exe'
    model = bundle / 'runtimes/glm/python.exe'
    inference = root / 'inference/test'
    def run(python, script, *arguments, guard=False):
        command = [str(python), '-B', '-X', 'utf8', '-c',
            "import sys,runpy;sys.path.insert(0,'scripts');runpy.run_path(sys.argv.pop(1),run_name='__main__')",
            'scripts/' + script, *map(str, arguments)]
        print('Stage', script, flush=True)
        lock = None
        try:
            if guard:
                import msvcrt
                path = Path(os.environ['LOCALAPPDATA']) / 'OfflineOCR/gpu.lock'
                path.parent.mkdir(parents=True, exist_ok=True)
                lock = path.open('a+b')
                if not lock.tell(): lock.write(b'0'); lock.flush()
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            subprocess.run(command, cwd=ROOT, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            if lock: lock.close()
    run(service, 'evaluate_geometry_gpu.py', '--bundle', bundle, '--manifest', manifest,
        '--output', inference, '--phase', 'all', '--mapper', 'none', '--test-code-lock', seal)
    run(args.rapid_python, 'evaluate_rapid_reference.py', '--manifest', manifest, '--output', inference / 'rapidtable',
        '--ocr', inference / 'ppocr', '--mapper', 'none', '--test-code-lock', seal,
        '--dependencies', ROOT / 'build/tableformer-next-20260915/runtime/rapid-dependencies')
    run(model, 'compare_tableformer_geometry.py', 'infer', '--manifest', manifest, '--output', inference / 'tableformer',
        '--ocr', inference / 'ppocr', '--test-code-lock', seal, guard=True)
    run(service, 'infer_geometry_real_results.py', '--manifest', manifest, '--bundle', bundle,
        '--output', root / 'adopted/test', '--test-code-lock', seal)
    for track in ('control', 'real-result'):
        replay = root / 'replay' / ('test-' + track)
        extra = ['--adopted-results', root / 'adopted/test'] if track == 'real-result' else []
        run(service, 'develop_tableformer_v3.py', 'replay', '--manifest', manifest, '--candidates', inference,
            '--tableformer', inference / 'tableformer', '--output', replay, '--test-seal', seal, *extra)
    # No test annotation file is opened before this point.
    for track in ('control', 'real-result'):
        run(service, 'develop_tableformer_v3.py', 'report', '--annotations', root / 'dataset/sealed/test.annotations.json',
            '--replay', root / 'replay' / ('test-' + track), '--output', root / 'reports' / ('test-' + track + '.json'))


if __name__ == '__main__':
    main()
