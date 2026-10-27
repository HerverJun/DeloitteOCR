"""Frozen manifests remain byte-identical while read views follow a restore map."""
import hashlib
import json
from pathlib import Path
import sys
import uuid

import numpy as np
from PIL import Image
import pytest

from image_platform.datasets import load_dataset
from image_platform.images import file_sha, preprocess
from image_platform.settings import Settings
from image_platform.store import Store


@pytest.mark.parametrize('external', [False, True])
def test_restored_dataset_uses_mapped_original_bytes_without_rewriting_frozen_manifest(tmp_path, monkeypatch, external):
    platform = Path(__file__).resolve().parents[2] / 'platform'
    monkeypatch.syspath_prepend(str(platform))
    data = tmp_path / '中文 DATA'
    old = Path('F:/missing-legacy-data-' + uuid.uuid4().hex)
    original = data / 'native/classification/uploads/original.png'
    original.parent.mkdir(parents=True)
    Image.new('RGB', (32, 24), 'blue').save(original)
    expected = hashlib.sha256(original.read_bytes()).hexdigest()
    prior = str(Path('F:/missing-external-' + uuid.uuid4().hex) / 'original.png') if external else str(old / original.relative_to(data))
    policy = {'allowed_drives': ['F:'], 'legacy_data_roots': [str(old)]}
    if external:
        policy['legacy_file_map'] = {prior: original.relative_to(data).as_posix()}
    config = tmp_path / 'clear.install.json'
    config.write_text(json.dumps({'schema_version': 1, 'installation_id': str(uuid.uuid4()),
        'mode': 'single', 'paths': {'program': str(platform.parent), 'assets': str(tmp_path/'assets'),
            'runtimes': str(Path(sys.executable).parent), 'tools': str(platform.parent),
            'data': str(data), 'cache': str(tmp_path/'cache'), 'temp': str(tmp_path/'temp'),
            'paddle_temp': str(tmp_path/'ascii-temp'), 'secrets': str(tmp_path/'secrets'), 'backup': str(tmp_path/'backup')},
        'network': {'transport': 'loopback-http', 'listen': '127.0.0.1',
            'public_origin': 'http://127.0.0.1:8889', 'portal_port': 8889, 'worker_port': 8890, 'postgres_port': 5432},
        'resources': {}, 'policy': policy}), encoding='utf-8')
    monkeypatch.setenv('CLEAR_INSTALL_CONFIG', str(config))
    settings = Settings(data/'native/classification')
    store = Store(settings)
    frozen = settings.data_dir / 'datasets/frozen.json'
    raw = json.dumps({'labels': [], 'items': [{'id': 'original', 'path': prior, 'sha256': expected}]}, ensure_ascii=False).encode('utf-8')
    frozen.write_bytes(raw)
    with store.connect(write=True) as db:
        db.execute('INSERT INTO projects VALUES(?,?,?,?,?,?)', ('p', 'test', '', '[]', None, 'test'))
        db.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?)', ('d', 'p', 'frozen', '[]', str(frozen), hashlib.sha256(raw).hexdigest(), '{}', 'test'))
    dataset, snapshot = load_dataset(store, 'd')
    assert snapshot['items'][0]['path'] == prior
    assert file_sha(prior) == expected
    actual = preprocess(prior, expected)
    assert actual.shape == (3, 224, 224) and np.isfinite(actual).all()
    assert np.array_equal(actual, preprocess(original, expected))
    assert frozen.read_bytes() == raw
    assert dataset['manifest_sha256'] == hashlib.sha256(raw).hexdigest()
