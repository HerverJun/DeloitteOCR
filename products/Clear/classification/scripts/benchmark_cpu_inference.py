"""Run inside the isolated CPU environment, after GPU performance experiments finish."""
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time

import numpy as np

from image_platform.images import preprocess
from image_platform.runtime import decide, session_for, validate_package
from image_platform.settings import Settings
from image_platform.store import Store


def main():
    assert importlib.util.find_spec('torch') is None, 'CPU environment must not contain torch'
    assert importlib.util.find_spec('timm') is None, 'CPU environment must not contain timm'
    store = Store(Settings.from_env())
    report = json.loads(Path('reports/flowers102-benchmark.json').read_text(encoding='utf-8'))
    dataset = store.one('SELECT manifest_path FROM datasets WHERE id=?', (report['dataset']['dataset_id'],))
    snapshot = json.loads(Path(dataset['manifest_path']).read_text(encoding='utf-8'))
    items = sorted((item for item in snapshot['items'] if item['split'] == 'test'), key=lambda item: item['sha256'])[:128]
    output = {'python': sys.version, 'torch_installed': False, 'timm_installed': False, 'device': 'CPU', 'threads': 4,
              'samples': 128, 'batch_size': 16, 'repetitions': 3, 'order': 'SHA-sorted first 128 test images',
              'environment_bytes': sum(p.stat().st_size for p in Path(sys.prefix).rglob('*') if p.is_file()),
              'size_scope': 'Files inside isolated venv; excludes shared Python base interpreter, model and pip cache',
              'dependencies': sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()), 'models': []}
    for encoder, model_id in report['recommendations'].items():
        row = store.one('SELECT package_path FROM models WHERE id=?', (model_id,))
        start = time.perf_counter()
        meta = validate_package(row['package_path'])
        session = session_for(Path(row['package_path']) / 'model.onnx', require_gpu=False)
        startup = time.perf_counter() - start
        golden = preprocess(items[0]['path'], items[0]['sha256'], 224, 224)[None]
        session.run(['logits'], {'images': golden})
        rates, durations, reference = [], [], None
        for _ in range(3):
            before = time.perf_counter()
            logits = []
            for start in range(0, len(items), 16):
                batch_start = time.perf_counter()
                batch = np.stack([preprocess(i['path'], i['sha256'], 224, 224) for i in items[start:start+16]])
                logits.append(session.run(['logits'], {'images': batch})[0])
                durations.append(time.perf_counter() - batch_start)
            rates.append(len(items) / (time.perf_counter() - before))
            predicted = [row['label_id'] for row in decide(np.concatenate(logits), meta)]
            if reference is None:
                reference = predicted
            assert predicted == reference
        result = {'encoder': encoder, 'model_id': model_id, 'providers': session.get_providers(), 'startup_seconds': startup,
                  'whole_input_pipeline_images_per_second': rates, 'median_images_per_second': statistics.median(rates),
                  'batch_latency_p50_seconds': float(np.percentile(durations, 50)), 'batch_latency_p95_seconds': float(np.percentile(durations, 95)),
                  'predictions': reference, 'logits': np.concatenate(logits).tolist(), 'labels': [item['label_id'] for item in items], 'torch_imported': 'torch' in sys.modules}
        output['models'].append(result)
        Path('reports/cpu-inference-benchmark.json').write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(encoder, result['median_images_per_second'], flush=True)
        del session


if __name__ == '__main__':
    main()
