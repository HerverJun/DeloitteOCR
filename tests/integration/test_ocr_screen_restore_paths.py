"""CPU-only real preparation/consumer checks for portable native screen batches."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from PIL import Image, ImageDraw

from scripts.backup_workspace import create, restore, verify


def screen_workspace(tmp_path, monkeypatch):
    native = Path(__file__).resolve().parents[3] / 'ocr/src'
    monkeypatch.syspath_prepend(str(native))
    from ocr_workbench.store import Store
    from ocr_workbench.imaging import add_image
    from ocr_workbench.task_queue import TaskQueue
    from ocr_workbench.screen_batches import ScreenBatches
    import ocr_workbench.store as store_module
    assert Path(store_module.__file__).resolve().is_relative_to(native)
    source = tmp_path / 'native-source'
    store = Store(source)
    project = store.project('Portable screen batch')['id']
    versions = []
    for number in range(2):
        path = tmp_path / f'input-{number}.png'
        image = Image.new('RGB', (240, 160), (255 - number, 255, 255))
        drawing = ImageDraw.Draw(image)
        for y in (30, 60, 90, 120):
            drawing.line((20, y, 220, y), fill='black', width=2)
        for x in (20, 120, 220):
            drawing.line((x, 30, x, 120), fill='black', width=2)
        drawing.text((30, 40), f'Fixture {number}', fill='black')
        image.save(path)
        versions.append(add_image(store, project, path.name, path)['active_version'])
    queue = TaskQueue(store, tmp_path)
    # No queue/thread/model starts. Real CPU preparation and _enqueue are used.
    manager = ScreenBatches(store, queue, tmp_path, hold_wal=False)
    batch = manager.submit(project, {'version_ids': versions, 'request_id': 'portable-screen-request',
        'policy': {'primary_engine': 'glm', 'max_upgrades': 0, 'perspective': False,
                   'layout_evidence': False}})['batch_id']
    manager.step()
    with store.transaction() as db:
        db.execute("UPDATE tasks SET status='failed',error='CPU fixture only' WHERE batch=?", (batch + '-stage-1-glm',))
    manager.step()
    manager.stop()
    return source, store, queue, manager, project, batch, versions


def protected_history(root):
    with closing(sqlite3.connect((root / 'workbench.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        return {'results': db.execute('SELECT id,original,edited FROM results ORDER BY id').fetchall(),
                'contexts': db.execute('SELECT task_id,raw FROM screen_contexts ORDER BY task_id').fetchall(),
                'candidates': db.execute('SELECT batch_id,input_version_id,candidates FROM screen_items ORDER BY input_version_id').fetchall()}


def test_restored_screen_originals_crops_and_retry_use_the_new_root(tmp_path, monkeypatch):
    source, store, queue, manager, project, batch, versions = screen_workspace(tmp_path, monkeypatch)
    # Historical result/provenance JSON is explicitly frozen before backup.
    # Its storage fixture does not claim any model execution or OCR accuracy.
    task = store.rows('SELECT task_id FROM screen_tasks ORDER BY task_id LIMIT 1')[0]['task_id']
    history = json.dumps({'historical_path': str(source / 'old-inference-input.png'), 'text': 'CPU fixture'})
    with store.transaction() as db:
        db.execute('INSERT INTO screen_contexts(task_id,batch_id,input_version_id,raw) VALUES(?,?,?,?)',
                   (task, batch, versions[0], history))
        db.execute('INSERT INTO results(id,task_id,original,edited,cursor,revision,updated) VALUES(?,?,?,?,0,0,?)',
                   ('history-result', task, history, history, 'CPU history fixture'))
    original_history = protected_history(source)
    preparations = {row['input_version_id']: json.loads(row['preparation']) for row in store.rows('SELECT * FROM screen_items')}
    links = store.rows('SELECT * FROM screen_tasks')
    archive, target = tmp_path / 'backup', tmp_path / 'restored'
    manifest = create(source, archive, kind='ocr')
    assert manifest['source_data_dir'] == str(source.resolve())
    assert verify(archive) == manifest
    # Make all old roots unavailable before production restore and consumers.
    hidden = tmp_path / 'old-source-offline'
    shutil.move(str(source), hidden)
    receipt = restore(archive, target)
    assert receipt['screen_input_paths_relocated'] == 2
    assert receipt['screen_prepared_paths_relocated'] == sum(len(info['prepared_images']) for info in preparations.values())
    assert receipt['screen_task_metadata_paths_relocated'] == len(links)
    assert protected_history(target) == original_history
    from ocr_workbench.store import Store
    from ocr_workbench.task_queue import TaskQueue
    from ocr_workbench.screen_batches import ScreenBatches
    reopened = Store(target)
    restored_queue = TaskQueue(reopened, tmp_path)
    restored = ScreenBatches(reopened, restored_queue, tmp_path, hold_wal=False)
    try:
        for version in versions:
            detail = restored.detail(project, batch, version)
            info = detail['preparation']
            original = reopened.file(reopened.one('versions', version)['path'])
            assert Path(info['input_path']) == original
            assert hashlib.sha256(original.read_bytes()).hexdigest() == info['input_sha256']
            for crop in info['prepared_images']:
                path = restored.file(project, batch, version, crop['id'])
                assert path == Path(crop['path']) and path.is_relative_to(target)
                relative = path.relative_to(target).as_posix()
                assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest['files'][relative]['sha256']
                with Image.open(path) as image:
                    image.verify()
        restored.action(project, batch, {'action': 'retry_failed', 'version_ids': versions,
                        'request_id': 'portable-screen-retry'})
        restored.step()
        retries = reopened.rows('SELECT prepared_path,metadata FROM screen_tasks WHERE attempt=1')
        assert retries
        for retry in retries:
            metadata = json.loads(retry['metadata'])
            path = reopened.file(retry['prepared_path'])
            assert Path(metadata['path']) == path
            assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata['prepared_pixel_sha256']
        assert protected_history(target) == original_history
        assert not restored_queue.thread  # Enqueued only; no GPU dispatch.
    finally:
        restored.stop()


@pytest.mark.parametrize('damage', ['outside_root', 'other_version_crop', 'other_input',
                                  'task_identity', 'task_pixel_hash', 'missing_crop', 'missing_source_root'])
def test_invalid_live_screen_references_never_publish_a_restore(tmp_path, monkeypatch, damage):
    source, store, queue, manager, project, batch, versions = screen_workspace(tmp_path, monkeypatch)
    rows = store.rows('SELECT * FROM screen_items ORDER BY position')
    first, other = [json.loads(row['preparation']) for row in rows]
    if damage == 'outside_root':
        first['prepared_images'][0]['path'] = str(tmp_path / 'foreign-root' / 'original.png')
    elif damage == 'other_version_crop':
        first['prepared_images'][0]['path'] = other['prepared_images'][0]['path']
    elif damage == 'other_input':
        first['input_path'] = other['input_path']
    elif damage == 'missing_crop':
        Path(first['prepared_images'][0]['path']).unlink()
    elif damage in {'task_identity', 'task_pixel_hash'}:
        link = store.rows('SELECT task_id,metadata FROM screen_tasks LIMIT 1')[0]
        metadata = json.loads(link['metadata'])
        metadata['batch_id' if damage == 'task_identity' else 'prepared_pixel_sha256'] = 'incorrect'
        with store.transaction() as db:
            db.execute('UPDATE screen_tasks SET metadata=? WHERE task_id=?', (json.dumps(metadata), link['task_id']))
    if damage in {'outside_root', 'other_version_crop', 'other_input'}:
        with store.transaction() as db:
            db.execute('UPDATE screen_items SET preparation=? WHERE batch_id=? AND input_version_id=?',
                       (json.dumps(first), batch, versions[0]))
    archive, target = tmp_path / 'backup', tmp_path / 'unpublished-restore'
    create(source, archive, kind='ocr')
    if damage == 'missing_source_root':
        manifest_path = archive / 'manifest.json'
        manifest = json.loads(manifest_path.read_text('utf-8'))
        del manifest['source_data_dir']
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    # This reaches semantic path validation, rather than only a checksum error.
    verify(archive)
    with pytest.raises(ValueError, match='screen|Screen'):
        restore(archive, target)
    assert not target.exists()
    assert not list(tmp_path.glob('unpublished-restore.pending-*'))
