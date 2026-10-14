"""Compare isolated CPU outputs with the same model on CUDA, outside timing runs."""
import json
from pathlib import Path

import numpy as np

from image_platform.images import preprocess
from image_platform.runtime import decide, session_for, validate_package
from image_platform.settings import Settings
from image_platform.store import Store


def main():
    path = Path('reports/cpu-inference-benchmark.json')
    cpu = json.loads(path.read_text(encoding='utf-8'))
    benchmark = json.loads(Path('reports/flowers102-benchmark.json').read_text(encoding='utf-8'))
    store = Store(Settings.from_env())
    dataset = store.one('SELECT manifest_path FROM datasets WHERE id=?', (benchmark['dataset']['dataset_id'],))
    snapshot = json.loads(Path(dataset['manifest_path']).read_text(encoding='utf-8'))
    items = sorted((i for i in snapshot['items'] if i['split'] == 'test'), key=lambda i: i['sha256'])[:128]
    tensors = [preprocess(i['path'], i['sha256'], 224, 224) for i in items]
    for row in cpu['models']:
        model = store.one('SELECT package_path FROM models WHERE id=?', (row['model_id'],))
        metadata = validate_package(model['package_path'])
        session = session_for(Path(model['package_path']) / 'model.onnx', True)
        logits = np.concatenate([session.run(['logits'], {'images': np.stack(tensors[i:i+16])})[0] for i in range(0, len(items), 16)])
        assert np.allclose(logits, row['logits'], atol=1e-4, rtol=1e-3), row['encoder']
        assert [d['label_id'] for d in decide(logits, metadata)] == row['predictions'], row['encoder']
        assert [(d['label_id'], d['status']) for d in decide(logits, metadata, True)] == [(d['label_id'], d['status']) for d in decide(row['logits'], metadata, True)], row['encoder']
        row['cuda_parity'] = {'passed': True, 'class_match': True, 'acceptance_match': True, 'max_abs_logit_difference': float(np.max(np.abs(logits - row['logits']))), 'samples': len(items)}
        del row['logits']
        del session
    path.write_text(json.dumps(cpu, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('All four isolated CPU models agree with CUDA on 128 real images.')


if __name__ == '__main__':
    main()
